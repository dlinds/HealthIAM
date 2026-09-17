# HR import format

Departments, job codes and positions can be loaded from CSV/TSV files, either through
**Admin → HR imports** (upload → preview → apply) or with the management command that a
scheduled job can call once an HR feed exists:

```
python manage.py import_hr --kind departments --file /feeds/departments.csv [--dry-run] [--deactivate-missing]
python manage.py import_hr --kind job_codes   --file /feeds/job_codes.csv
python manage.py import_hr --kind positions   --file /feeds/positions.csv
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

## Suggested feed order

1. Departments
2. Job codes
3. Positions (if the HR system exports valid pairs)

Run each with `--deactivate-missing` only when the file is a complete extract.
