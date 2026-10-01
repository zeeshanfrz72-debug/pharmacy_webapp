# PythonAnywhere security and rollout steps

The repository has no deployment secrets. `pharmacy_app/settings.py` now requires `DJANGO_SECRET_KEY`, uses `DEBUG=False` by default, rejects wildcard hosts, and reads the single permitted username from `APP_OWNER_USERNAME`.

## Set private configuration first

Use PythonAnywhere's web app WSGI configuration or a private environment file outside Git. Set:

```text
DJANGO_SECRET_KEY=<new random value generated privately>
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=z33shan.pythonanywhere.com
APP_OWNER_USERNAME=<the existing owner's exact username>
DJANGO_SESSION_COOKIE_SECURE=True
DJANGO_CSRF_COOKIE_SECURE=True
DJANGO_SECURE_SSL_REDIRECT=False
DJANGO_HSTS_SECONDS=0
```

Generate the Django key in a private PythonAnywhere console with `python -c "import secrets; print(secrets.token_urlsafe(64))"` and put it directly into PythonAnywhere configuration. Do not commit it or send it in chat. Rotating it invalidates existing sessions. The owner should also change the password that was shared in this conversation directly through the app before deployment.

The former Django key was committed in the public Git history. Removing it from the current settings file does not remove old Git objects; the new private key must be independent of it. Consider a coordinated history purge if the repository's published history must also be scrubbed.

## HTTPS and HSTS gate

PythonAnywhere's HTTPS certificate is available for `pythonanywhere.com` subdomains, but the app currently responds with `200 OK` on plain HTTP. In the PythonAnywhere Web tab, turn on **Force HTTPS**, reload the web app, and verify from outside PythonAnywhere that an `http://` request redirects to the matching `https://` URL and that the HTTPS login page loads. Only then set `DJANGO_HSTS_SECONDS=31536000` and reload again. Verify the HTTPS response includes `Strict-Transport-Security` and that the plain HTTP request still redirects. Leave HSTS at `0` until those checks pass.

## Back up, validate, then deploy

1. Compare the PythonAnywhere virtualenv's current `pip freeze` with `requirements.txt`; install the pinned requirements in that virtualenv.
2. Make a consistent SQLite backup outside the repository and verify it can be restored to a separate test path. Never use the production database for migration experiments or load testing.
3. On the isolated copy, run `python manage.py migrate`, `python manage.py check --deploy`, `python manage.py test ledger.tests`, and `python manage.py collectstatic --noinput`.
4. Confirm the migration preserved every preexisting current balance, including the existing effect of rows already marked soft-deleted. Migration `0006` assigns one audit batch to each old ledger row and adds no balance offsets.
5. After the application and accounting checks pass on staging or a sanitized copy, take a fresh production backup, pull the reviewed feature branch, migrate, collect static files, and reload using PythonAnywhere's normal Django deployment workflow.
6. Use production only for read-only sign-in and page smoke checks. Do not run reversal, restore, destructive, or load-test requests against production.

The app deliberately fails closed for logged-in accounts other than `APP_OWNER_USERNAME`. Keep that variable set to the existing owner account, and do not create public accounts. Login failures are tracked in the database by django-axes with a five-failure, fifteen-minute rolling window.

## Offline queue behavior

The service worker caches same-origin static assets only; it never stores page or API responses. Pending transactions remain in account-scoped IndexedDB through sign-out and session expiry. Sync removes an item only after a JSON success response says the batch was committed or had already been committed. Validation failures stay queued for correction; authentication and CSRF failures pause sync until the owner signs in again.
