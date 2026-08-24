"""
Automated Test Runner for All Phase C3 and Phase C4 Test Suites.
Executes each phase test suite in isolated subprocesses, aggregating test statistics,
execution times, and pass/fail statuses.
"""

import os
import sys
import time
import subprocess

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

TEST_SUITES = [
    ("Phase C3 (Catalog & Provenance)", "tests/extensions/test_phase_c3.py"),
    ("Phase C3.1 (Catalog Polish & Navigation)", "tests/extensions/test_phase_c3_1.py"),
    ("Phase C3.2 (Catalog Performance)", "tests/extensions/test_phase_c3_2.py"),
    ("Phase C4.1 (Creator Indexing & Storage)", "tests/extensions/test_phase_c4_1.py"),
    ("Phase C4.2 (Creator Browser Service)", "tests/extensions/test_phase_c4_2.py"),
    ("Phase C4.4 (Creator Browser Templates & CDP)", "tests/extensions/test_phase_c4_4.py"),
    ("Phase C4.5 (Metron Comparison Service)", "tests/extensions/test_phase_c4_5.py"),
    ("Phase C4.6 (Metron Comparison UI & Cache)", "tests/extensions/test_phase_c4_6.py"),
    ("Phase C4.8 (Creator Identity Resolution Service)", "tests/extensions/test_phase_c4_8.py"),
    ("Phase C4.9 (Candidate Discovery Service)", "tests/extensions/test_phase_c4_9.py"),
    ("Phase C4.10 (Decision Actions & CSRF Controller)", "tests/extensions/test_phase_c4_10.py"),
    ("Phase C4.11 (Decision History Service & UI)", "tests/extensions/test_phase_c4_11.py"),
    ("Phase C4.12 (Creator Identity Registry Service & UI)", "tests/extensions/test_phase_c4_12.py"),
    ("Phase C4.13 (Conflict Analysis & Explanation)", "tests/extensions/test_phase_c4_13.py"),
    ("Phase C4.15 (Keep-Existing Conflict Resolution)", "tests/extensions/test_phase_c4_15.py"),
    ("Phase C4.16 (Conflict Transfer & Safe Reversal)", "tests/extensions/test_phase_c4_16.py"),
    ("Phase C4.17 (End-to-End Acceptance Audit)", "tests/extensions/test_phase_c4_17.py"),
]


def run_all_suites():
    print("=" * 80)
    print("STARTING ALL PHASE C3 & C4 ACCEPTANCE REGRESSION SUITES")
    print("=" * 80)

    total_suites = len(TEST_SUITES)
    passed_suites = 0
    failed_suites = 0
    total_tests_run = 0
    suite_results = []
    overall_start = time.time()

    for title, rel_path in TEST_SUITES:
        full_path = os.path.join(REPO_ROOT, rel_path)
        if not os.path.exists(full_path):
            print(f"[-] SKIPPED: {title} ({rel_path} not found)")
            suite_results.append((title, "SKIPPED", 0, 0.0, "File not found"))
            continue

        print(f"[*] Running: {title} ...", end=" ", flush=True)
        t0 = time.time()
        res = subprocess.run(
            [sys.executable, full_path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True
        )
        duration = round(time.time() - t0, 3)

        output = res.stderr + "\n" + res.stdout
        # Extract ran tests count
        tests_count = 0
        for line in output.splitlines():
            if "Ran " in line and " test" in line:
                try:
                    tests_count = int(line.split("Ran ")[1].split(" test")[0])
                except Exception:
                    pass

        if res.returncode == 0:
            print(f"[OK] ({tests_count} tests in {duration}s)")
            passed_suites += 1
            total_tests_run += tests_count
            suite_results.append((title, "PASSED", tests_count, duration, None))
        else:
            print(f"[FAILED] in {duration}s (exit {res.returncode})")
            print("-" * 60)
            print(output.strip())
            print("-" * 60)
            failed_suites += 1
            total_tests_run += tests_count
            suite_results.append((title, "FAILED", tests_count, duration, output))

    overall_duration = round(time.time() - overall_start, 2)
    print("\n" + "=" * 80)
    print("TEST SUITE EXECUTION SUMMARY")
    print("=" * 80)
    print(f"{'Suite Name':<52} | {'Status':<8} | {'Tests':<6} | {'Time'}")
    print("-" * 80)
    for title, status, count, dur, err in suite_results:
        print(f"{title:<52} | {status:<8} | {count:<6} | {dur:.3f}s")
    print("=" * 80)
    print(f"Total Suites: {total_suites} | Passed: {passed_suites} | Failed: {failed_suites}")
    print(f"Total Tests Run: {total_tests_run} | Total Duration: {overall_duration}s")
    print("=" * 80)

    if failed_suites > 0:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == '__main__':
    run_all_suites()
