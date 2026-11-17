# Notebook assessment service

The assessment notebook submits to the Python grader on port 8081 through nginx.
`POST /grade/part1` runs the checks and saves a result in the shared
assessment-results volume. `GET /health` checks the required inputs.
The notebook displays the result; no DLI LMS button or Go runner is required.

`grader/rubric.json` defines checks and thresholds. `grader/answer_keys/` holds
reference answers and event labels. Update event labels when changing the videos;
the temporary empty label sample is not verified ground truth.

The service reads student answers from `notebooks/work/assessment/` through its
container mount. It runs Python in isolated mode and retains saved results across
restarts. Container paths remain under `/dli` for compatibility.

Run offline regression checks from the repository root:

```bash
PYTHONPATH=notebooks:assessment python3 -m pytest assessment/tests
```
