"""Gunicorn settings for the deployed web service.

Gunicorn picks this file up automatically from the working directory, so the
start command stays `gunicorn loadpairing.web:application` and the port does
not have to be expanded by a shell.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("WEB_THREADS", "4"))
timeout = int(os.environ.get("WEB_TIMEOUT", "120"))
accesslog = "-"
errorlog = "-"
