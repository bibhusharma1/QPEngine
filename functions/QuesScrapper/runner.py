# Run scraper checks, then extract PDFs configured in .env.
import subprocess
import sys

def run_step(label, command):
    print(f"\n=== {label} ===")
    result = subprocess.run(command)
    if result.returncode != 0:
        print(f"\nFAILED: {label} (exit code {result.returncode})", file=sys.stderr)
        sys.exit(result.returncode)

def main():
    tests_only = "--tests-only" in sys.argv
    run_step("1/2 Test suite", [sys.executable, "-m", "pytest", "tests/Scrapper/", "-q"])
    if tests_only:
        return
    run_step("2/2 Scrape PDFs from .env", [sys.executable, "-m", "functions.QuesScrapper.ScrapePipeLIne.pipeline"])
    print("\nScrape complete.")

if __name__ == "__main__":
    main()
