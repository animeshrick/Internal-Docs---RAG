"""
limits.py - how big an input may be.

These are only guards against huge or malformed input, so the request is rejected
before it reaches a rule or the database. The real rules live in the services:
password strength in identity/auth.py, leave limits in hr/leave/service.py.

They sit here rather than in main.py because both the request models and the
route handlers need them, and the models must not import main.
"""
MAXIMUM_QUESTION_LENGTH = 1000
MAXIMUM_PASSWORD_INPUT_LENGTH = 200
MAXIMUM_DOCUMENT_BYTES = 5 * 1024 * 1024
ALLOWED_DOCUMENT_TYPES = ("application/pdf", "image/png", "image/jpeg")
