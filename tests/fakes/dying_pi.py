"""A stand-in for a pi that crashes at startup: prints the real cause to
stderr (like pi does for a bad model config) and exits non-zero without ever
answering the prompt. Used to test that the process-death error surfaces the
exit code and the stderr tail."""

import sys

print("Error: Invalid URL: $LLM_BASE_URL", file=sys.stderr)
print("    at file:///pi/dist/models.js:42", file=sys.stderr)
sys.stderr.flush()
sys.exit(7)
