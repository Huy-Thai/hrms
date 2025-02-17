# Copyright (c) 2019, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
import requests
import json
import frappe
import datetime
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_datetime, get_link_to_form, nowdate, now_datetime, get_time, getdate
from datetime import datetime as dt, timedelta
from dateutil.relativedelta import relativedelta

from hrms.hr.doctype.shift_assignment.shift_assignment import (
	get_actual_start_end_datetime_of_shift,
)
from hrms.hr.utils import validate_active_employee
from hrms.utils import config_env_service, get_all_date_in_month

class EmployeeCheckin(Document):
	def validate(self):
		validate_active_employee(self.employee)
		self.validate_duplicate_log()
		self.fetch_shift()

	def validate_duplicate_log(self):
		doc = frappe.db.exists(
			"Employee Checkin",
			{
				"employee": self.employee,
				"time": self.time,
				"name": ("!=", self.name),
				"log_type": self.log_type,
			},
		)
		if doc:
			doc_link = frappe.get_desk_link("Employee Checkin", doc)
			frappe.throw(
				_("This employee already has a log with the same timestamp.{0}").format("<Br>" + doc_link)
			)

	def fetch_shift(self):
		shift_actual_timings = get_actual_start_end_datetime_of_shift(
			self.employee, get_datetime(self.time), True
		)
		if shift_actual_timings:
			if (
				shift_actual_timings.shift_type.determine_check_in_and_check_out
				== "Strictly based on Log Type in Employee Checkin"
				and not self.log_type
				and not self.skip_auto_attendance
			):
				frappe.throw(
					_("Log Type is required for check-ins falling in the shift: {0}.").format(
						shift_actual_timings.shift_type.name
					)
				)
			if not self.attendance:
				self.shift = shift_actual_timings.shift_type.name
				self.shift_actual_start = shift_actual_timings.actual_start
				self.shift_actual_end = shift_actual_timings.actual_end
				self.shift_start = shift_actual_timings.start_datetime
				self.shift_end = shift_actual_timings.end_datetime
		else:
			self.shift = None


@frappe.whitelist()
def add_log_based_on_employee_field(
	employee_field_value,
	timestamp,
	device_id=None,
	log_type=None,
	skip_auto_attendance=0,
	employee_fieldname="attendance_device_id",
):
	"""Finds the relevant Employee using the employee field value and creates a Employee Checkin.

	:param employee_field_value: The value to look for in employee field.
	:param timestamp: The timestamp of the Log. Currently expected in the following format as string: '2019-05-08 10:48:08.000000'
	:param device_id: (optional)Location / Device ID. A short string is expected.
	:param log_type: (optional)Direction of the Punch if available (IN/OUT).
	:param skip_auto_attendance: (optional)Skip auto attendance field will be set for this log(0/1).
	:param employee_fieldname: (Default: attendance_device_id)Name of the field in Employee DocType based on which employee lookup will happen.
	"""

	if not employee_field_value or not timestamp:
		frappe.throw(_("'employee_field_value' and 'timestamp' are required."))

	employee = frappe.db.get_values(
		"Employee",
		{employee_fieldname: employee_field_value},
		["name", "employee_name", employee_fieldname],
		as_dict=True,
	)
	if employee:
		employee = employee[0]
	else:
		frappe.throw(
			_("No Employee found for the given employee field value. '{}': {}").format(
				employee_fieldname, employee_field_value
			)
		)

	doc = frappe.new_doc("Employee Checkin")
	doc.employee = employee.name
	doc.employee_name = employee.employee_name
	doc.time = timestamp
	doc.device_id = device_id
	doc.log_type = log_type
	if cint(skip_auto_attendance) == 1:
		doc.skip_auto_attendance = "1"
	doc.insert()

	return doc


def mark_attendance_and_link_log(
	logs,
	attendance_status,
	attendance_date,
	working_hours=None,
	late_entry=False,
	early_exit=False,
	in_time=None,
	out_time=None,
	shift=None,
):
	"""Creates an attendance and links the attendance to the Employee Checkin.
	Note: If attendance is already present for the given date, the logs are marked as skipped and no exception is thrown.

	:param logs: The List of 'Employee Checkin'.
	:param attendance_status: Attendance status to be marked. One of: (Present, Absent, Half Day, Skip). Note: 'On Leave' is not supported by this function.
	:param attendance_date: Date of the attendance to be created.
	:param working_hours: (optional)Number of working hours for the given date.
	"""
	log_names = [x.name for x in logs]
	employee = logs[0].employee

	if attendance_status == "Skip":
		skip_attendance_in_checkins(log_names)
		return None

	elif attendance_status in ("Present", "Absent", "Half Day"):
		try:
			frappe.db.savepoint("attendance_creation")
			attendance = frappe.new_doc("Attendance")
			attendance.update(
				{
					"doctype": "Attendance",
					"employee": employee,
					"attendance_date": attendance_date,
					"status": attendance_status,
					"working_hours": working_hours,
					"shift": shift,
					"late_entry": late_entry,
					"early_exit": early_exit,
					"in_time": in_time,
					"out_time": out_time,
				}
			).submit()

			if attendance_status == "Absent":
				attendance.add_comment(
					text=_("Employee was marked Absent for not meeting the working hours threshold.")
				)

			update_attendance_in_checkins(log_names, attendance.name)
			return attendance

		except frappe.ValidationError as e:
			handle_attendance_exception(log_names, e)

	else:
		frappe.throw(_("{} is an invalid Attendance Status.").format(attendance_status))


def calculate_working_hours_by_shift_type(logs):
	lunch_time = 1.5
	total_hours = 0
	in_time = out_time = None
	is_saturday = dt.today().weekday() == 5
	START_MIDDAY = datetime.time(12, 0, 0)
	END_MIDDAY = datetime.time(13, 30, 0)

	date = getdate()
	saturday_shift_end = dt.combine(date, get_time("12:00:00")) # saturday shift end is specific day so need config manually
	shift_start = logs[0].shift_start
	shift_end = logs[0].shift_end if not is_saturday else saturday_shift_end

	first_in_log_index = find_index_in_dict(logs, "log_type", "IN")
	first_in_log = (
		logs[first_in_log_index] if first_in_log_index or first_in_log_index == 0 else None
	)

	last_out_log_index = find_index_in_dict(reversed(logs), "log_type", "OUT")
	last_out_log = (
		logs[len(logs) - 1 - last_out_log_index]
		if last_out_log_index or last_out_log_index == 0
		else None
	)

	if first_in_log and last_out_log:
		in_time, out_time = first_in_log.time, last_out_log.time
		if in_time >= shift_end:
			return total_hours, in_time, out_time

		total_hours = time_diff_in_hours(in_time, out_time)
		if in_time < shift_start:
			total_hours -= time_diff_in_hours(in_time, shift_start)
		if out_time > shift_end:
			total_hours -= time_diff_in_hours(shift_end, out_time)

		last_out_time = datetime.time(out_time.hour, out_time.minute, 0)
		is_midday_time_range = time_in_range(START_MIDDAY, END_MIDDAY, last_out_time)
		is_after_early_afternoon = last_out_time > END_MIDDAY

		if not is_midday_time_range and is_after_early_afternoon and not is_saturday:
			total_hours -= lunch_time

	return total_hours, in_time, out_time

def calculate_working_hours(logs, check_in_out_type, working_hours_calc_type):
	"""Given a set of logs in chronological order calculates the total working hours based on the parameters.
	Zero is returned for all invalid cases.

	:param logs: The List of 'Employee Checkin'.
	:param check_in_out_type: One of: 'Alternating entries as IN and OUT during the same shift', 'Strictly based on Log Type in Employee Checkin'
	:param working_hours_calc_type: One of: 'First Check-in and Last Check-out', 'Every Valid Check-in and Check-out'
	"""
	total_hours = 0
	in_time = out_time = None
	if check_in_out_type == "Alternating entries as IN and OUT during the same shift":
		in_time = logs[0].time
		if len(logs) >= 2:
			out_time = logs[-1].time
		if working_hours_calc_type == "First Check-in and Last Check-out":
			# assumption in this case: First log always taken as IN, Last log always taken as OUT
			total_hours = time_diff_in_hours(in_time, logs[-1].time)
		elif working_hours_calc_type == "Every Valid Check-in and Check-out":
			logs = logs[:]
			while len(logs) >= 2:
				total_hours += time_diff_in_hours(logs[0].time, logs[1].time)
				del logs[:2]

	elif check_in_out_type == "Strictly based on Log Type in Employee Checkin":
		if working_hours_calc_type == "First Check-in and Last Check-out":
			first_in_log_index = find_index_in_dict(logs, "log_type", "IN")
			first_in_log = (
				logs[first_in_log_index] if first_in_log_index or first_in_log_index == 0 else None
			)
			last_out_log_index = find_index_in_dict(reversed(logs), "log_type", "OUT")
			last_out_log = (
				logs[len(logs) - 1 - last_out_log_index]
				if last_out_log_index or last_out_log_index == 0
				else None
			)
			if first_in_log and last_out_log:
				in_time, out_time = first_in_log.time, last_out_log.time
				total_hours = time_diff_in_hours(in_time, out_time)
		elif working_hours_calc_type == "Every Valid Check-in and Check-out":
			in_log = out_log = None
			for log in logs:
				if in_log and out_log:
					if not in_time:
						in_time = in_log.time
					out_time = out_log.time
					total_hours += time_diff_in_hours(in_log.time, out_log.time)
					in_log = out_log = None
				if not in_log:
					in_log = log if log.log_type == "IN" else None
					if in_log and not in_time:
						in_time = in_log.time
				elif not out_log:
					out_log = log if log.log_type == "OUT" else None

			if in_log and out_log:
				out_time = out_log.time
				total_hours += time_diff_in_hours(in_log.time, out_log.time)

	return total_hours, in_time, out_time


def time_diff_in_hours(start, end):
	return round(float((end - start).total_seconds()) / 3600, 2)


def find_index_in_dict(dict_list, key, value):
	return next((index for (index, d) in enumerate(dict_list) if d[key] == value), None)


def handle_attendance_exception(log_names: list, error_message: str):
	frappe.db.rollback(save_point="attendance_creation")
	frappe.clear_messages()
	skip_attendance_in_checkins(log_names)
	add_comment_in_checkins(log_names, error_message)


def add_comment_in_checkins(log_names: list, error_message: str):
	text = "{0}<br>{1}".format(frappe.bold(_("Reason for skipping auto attendance:")), error_message)


def time_in_range(start, end, value):
	"""Return true if value is in the range [start, end]"""
	if start <= end:
		return start <= value <= end
	else:
		return start <= value or value <= end


def add_comment_in_checkins(log_names, duplicate, overlapping):
	if duplicate:
		text = _("Auto Attendance skipped due to duplicate attendance record: {}").format(
			get_link_to_form("Attendance", duplicate[0].name)
		)
	else:
		text = _("Auto Attendance skipped due to overlapping attendance record: {}").format(
			get_link_to_form("Attendance", overlapping.name)
		)

	for name in log_names:
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Comment",
				"reference_doctype": "Employee Checkin",
				"reference_name": name,
				"content": text,
			}
		).insert(ignore_permissions=True)


def skip_attendance_in_checkins(log_names: list):
	EmployeeCheckin = frappe.qb.DocType("Employee Checkin")
	(
		frappe.qb.update(EmployeeCheckin)
		.set("skip_auto_attendance", 1)
		.where(EmployeeCheckin.name.isin(log_names))
	).run()


def update_attendance_in_checkins(log_names: list, attendance_id: str):
	EmployeeCheckin = frappe.qb.DocType("Employee Checkin")
	(
		frappe.qb.update(EmployeeCheckin)
		.set("attendance", attendance_id)
		.where(EmployeeCheckin.name.isin(log_names))
	).run()


def notification_employee_with_logtype(logType):
	now = nowdate()
	employeesPass = []
	notifications = {}

	config = config_env_service()
	employee_docs = frappe.db.get_all(
    "Employee",
    filters={
			"status": ['=', 'Active'],
		},
    fields=[
			"employee",
			"employee_name",
			"user_id",
		],
	)

	for emp in employee_docs:
		checkin_docs = frappe.db.get_all(
			"Employee Checkin",
			filters={
				"employee": emp.employee,
				"created_at": ['=', now],
			},
			order_by='time desc',
			fields=['log_type']
		)

		if not checkin_docs:
			notifications[emp.user_id] = "IN"
			continue	

		latest = checkin_docs[0].log_type
		if logType == "IN" and latest == "IN": 
			employeesPass.append(emp.user_id)
			continue

		if logType == "OUT" and latest == "IN":
			notifications[emp.user_id] = "OUT"
			continue

		if logType == "OUT" and latest == "OUT":
			employeesPass.append(emp.user_id)
			continue

		if logType == "IN" and latest == "OUT":
			notifications[emp.user_id] = "IN"
			continue

	url = config["msteam_bot"]
	payload = {"type": "CHECK_IN", "payloads": [json.dumps(notifications)]}

	response = requests.post(url=url, json=payload)
	result = response.text
	print(result)
	# Handler push message error here


def summarize_attendances_leaves_today():
  now = getdate()
  next_month = getdate() + relativedelta(months=+1)
  config = config_env_service()
	
  first_day_in_month = get_all_date_in_month(now.month, now.year)[0]
  last_day_in_next_month = get_all_date_in_month(next_month.month, next_month.year)[-1]

  empLeaves = {}
  empCheckIns = {}
  empNotCheckIns = {}
  empLateEntry = {}
  notifications = {}

  employee_docs = frappe.db.get_all(
    "Employee",
    filters={
			"status": ['=', 'Active'],
		},
    fields=[
			"employee",
			"employee_name",
			"user_id",
		],
	)
  
  for emp in employee_docs:
    leaves = frappe.db.get_all(
		"Leave Application",
		fields=[
			"name",
			"leave_type",
			"from_date",
			"to_date",
		],
		filters={
			"employee": emp.employee,
			"from_date": (">=", first_day_in_month),
			"to_date": ("<=", last_day_in_next_month),
		},
	)
    
    if leaves:
      for dic in leaves:
        from_date = getdate(dic["from_date"])
        to_date = getdate(dic["to_date"])
        is_leave_today = time_in_range(from_date, to_date, now)
        if is_leave_today:
          empLeaves[emp.user_id] = emp.employee_name
      continue

    checkin_docs = frappe.db.get_all(
		"Employee Checkin",
		filters={
			"employee": emp.employee,
			"created_at": ['=', now],
		},
		order_by='time desc',
		fields=[
			"time",
			"log_type",
			"shift_start",
		]
	)

    if not checkin_docs:
      empNotCheckIns[emp.user_id] = emp.employee_name
      continue

    first_in_log_index = find_index_in_dict(checkin_docs, "log_type", "IN")
    first_in_log = (checkin_docs[first_in_log_index] if first_in_log_index or first_in_log_index == 0 else None)
    if first_in_log:
      if (first_in_log.time > checkin_docs[0].shift_start + timedelta(minutes=15)):
        empLateEntry[emp.user_id] = emp.employee_name
      empCheckIns[emp.user_id] = emp.employee_name

  notifications["leaves"] = empLeaves
  notifications["notCheckIns"] = empNotCheckIns
  notifications["lateEntries"] = empLateEntry
  notifications["checkIns"] = empCheckIns

  url = config["msteam_bot"]
  payload = {"type": "SUMMARIZE_ATTENDANCES_LEAVES_TODAY", "payloads": [json.dumps(notifications)]}

  response = requests.post(url=url, json=payload)
  result = response.text
  print(result)
	# Handler push message error here


def employee_auto_checkout():
	now = nowdate()
	timestamp = now_datetime().__str__()[:-7]
	config = config_env_service()

	employee_doc = frappe.db.get_all(
	"Employee",
	filters={
			"status": ['=', 'Active'],
		},
	fields=[
			"employee",
			"employee_name",
		]
	)

	for emp in employee_doc:
		checkin_docs = frappe.db.get_all(
			"Employee Checkin",
			filters={
				"employee": emp.employee,
				"created_at": ['=', now],
			},
			order_by='time desc',
			fields=['log_type']
		)

		if not checkin_docs:
			continue	

		latest = checkin_docs[0].log_type
		if latest == "OUT":
			continue

		doc = frappe.new_doc("Employee Checkin")
		doc.employee = emp.employee
		doc.employee_name = emp.employee_name
		doc.time = timestamp
		doc.created_at = now
		doc.device_id = config["server_ip"]
		doc.log_type = "OUT"
		doc.auto_check_out = "1"
		doc.insert()
	frappe.db.commit()
	return True


def process_notification_employee_with_check_IN():
	notification_employee_with_logtype("IN")


def process_notification_employee_with_check_OUT():
	notification_employee_with_logtype("OUT")


def process_employee_auto_checkout():
	employee_auto_checkout()


def process_notification_to_bo_at_eleven_hours():
	summarize_attendances_leaves_today()
