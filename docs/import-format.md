# HR import format

Departments, job codes, positions and people can be loaded from CSV/TSV files, either
through **Admin → HR imports** (upload → preview → apply) or with the management command that
a scheduled job can call once an HR feed exists:

```
python manage.py import_hr --kind departments --file /feeds/departments.csv [--dry-run] [--deactivate-missing]
python manage.py import_hr --kind job_codes   --file /feeds/job_codes.csv
python manage.py import_hr --kind positions   --file /feeds/positions.csv
python manage.py import_hr --kind people      --file /feeds/people.csv
```

## General rules

- Header row required. Comma, tab, semicolon or pipe delimited; UTF-8 (BOM tolerated).
- Header names are case-insensitive; spaces and dashes become underscores.
- Codes must be exactly four digits. Keep leading zeros (`0100`, not `100`).
- Rows are **upserted by code**: new codes are created, changed names are updated,
  inactive records present in the file are reactivated, unchanged rows are reported as such.
- Imported records are marked `source = HR feed`.
- With **deactivate missing** on, HR-sourced records that are absent from the file are
  inactivated. Manual records are never touched.
- Rows with errors (bad code, missing name, duplicate code, unknown department) are
  skipped and listed; the rest of the file still imports.
- A preview is an exact dry run: the import runs inside a transaction that is rolled back.

## Departments

| Column | Required | Aliases |
|---|---|---|
| `code` | yes | `department_code`, `dept_code`, `dept` |
| `name` | yes | `department`, `department_name`, `dept_name` |

```
code,name
0100,Nursing
0200,Pharmacy
```

## Job codes

| Column | Required | Aliases |
|---|---|---|
| `code` | yes | `job_code`, `jobcode`, `job` |
| `title` | yes | `job_title`, `jobtitle`, `description`, `name` |

```
code,title
7000,Registered Nurse
7100,Pharmacist
```

## Positions

Either two code columns or one combined code. The department and job code must already exist.

| Column | Required | Aliases |
|---|---|---|
| `department_code` | one of | `dept_code`, `department`, `dept` |
| `job_code` | one of | `job`, `jobcode` |
| `position_code` | one of | `position`, `code` — format `DDDD-JJJJ` |
| `title` | no | `name`, `position_title` |

```
department_code,job_code,title
0100,7000,Staff Nurse
0100,7002,Nurse Manager
```

or

```
position_code
0100-7000
0100-7002
```

## People

One row per employee, upserted by `employee_id`. Every position named must already exist
(run the positions feed first). Header aliases are case-insensitive.

| Column | Required | Aliases / values |
|---|---|---|
| `employee_id` | yes | `emp_id`, `employee_number`, `employee`, `id` |
| `first_name`, `last_name` | yes | `first` / `given_name`; `last` / `surname` / `family_name` |
| `middle_name`, `suffix`, `preferred_name` | no | `middle`; `preferred` / `nickname` |
| `email`, `phone`, `location` | no | `work_email` / `mail`; `work_phone`; `site` / `campus` |
| `position_code` | yes* | `position`, `primary_position` — `DDDD-JJJJ`; or `department_code` + `job_code`. *Not needed on a `terminated` row. |
| `alternate_positions` | no | `alternates`, `secondary_positions` — semicolon-separated `DDDD-JJJJ` codes |
| `person_type` | no | `type`, `worker_type` — a person type code (default `employee`) |
| `status` | no | `employment_status` — `active` (default), `leave`, `terminated` |
| `hire_date`, `separation_date`, `position_start_date` | no | `hired`; `termination_date` / `term_date`; `effective_date` / `job_start_date` — `YYYY-MM-DD` |
| `manager_employee_id` | no | `manager`, `manager_id`, `supervisor`, `supervisor_id` — resolved after every row is loaded |

```
employee_id,first_name,last_name,preferred_name,email,position_code,alternate_positions,status,hire_date,position_start_date,manager_employee_id
E1002,Daniel,Okoro,Dan,daniel.okoro@example.org,0100-7000,0300-7000,active,2019-03-04,,E1001
E1003,Hannah,Weiss,,hannah.weiss@example.org,0100-7000,,leave,2021-06-14,,E1001
E1016,Paul,Grant,,,,,terminated,2018-01-08,,
```

What each row does:

- **New employee ID** → the person is created (`source = HR feed`) with a primary assignment
  starting on `position_start_date` (or today) and one alternate assignment per code listed.
  A terminated person the database does not know is *skipped*, never created inactive.
- **Changed legal name** → the old name is kept as a former name (effective on
  `position_start_date` or today) so search still finds the person under it; a changed
  `preferred_name` is updated in place.
- **Changed primary position** → the current HR-sourced primary assignment ends the day before
  `position_start_date` (reason *transfer*) and the new one starts. The feed refuses the row
  when the person holds a primary position added by hand, and when the transfer date is not
  after the current assignment's start.
- **Alternate positions** → HR-sourced alternates not in the list are ended; listed codes not
  yet held are added. Assignments added by hand (a student rotation on an employee) are never
  touched.
- **`status`** → `leave` sets and `active` clears *on leave* (expected access is suspended while
  on leave); `terminated` ends every open assignment on `separation_date` (or today) and marks
  the person inactive. An inactive person who reappears with `active` is reactivated.
- **`manager_employee_id`** → linked in a second pass; an unknown ID is a *warning*, not an
  error.
- **Empty optional columns** leave the current value alone.
- A person somebody entered by hand with the same employee ID is adopted by the feed
  (`source` becomes *HR feed*): the employee ID says it is the same person. A primary
  assignment added by hand on the position the file names is adopted with them; one on a
  different position is a row error until it is ended in HealthIAM.
- With **deactivate missing** on, HR-sourced active people whose employee ID is not in the
  file at all are marked inactive as of today. A row that failed validation still counts as
  present, so a typo never deactivates anyone.

Every write is audited with the reason `HR import #<batch>` (or `HR people import` from the
command line) and no actor when run from the command line.

## Suggested feed order

1. Departments
2. Job codes
3. Positions (if the HR system exports valid pairs)
4. People

Run each with `--deactivate-missing` only when the file is a complete extract.
