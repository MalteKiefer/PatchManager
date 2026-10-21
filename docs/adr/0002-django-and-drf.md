# 2. Django with Django REST Framework

* Status: accepted
* Date: 2026-10-21

## Context

We need server rendered UI, an ORM with strong PostgreSQL support, mature auth and
security middleware, migrations, i18n and a REST API with an OpenAPI 3.1 schema.

## Decision

Django 6.1 for the web application. Django REST Framework with drf-spectacular
(`OAS_VERSION = "3.1.0"`) for the API, from Phase 1 on.

Django Ninja was considered. It generates OpenAPI 3.1 natively, but DRF offers more
mature building blocks for object level permissions, filtering, throttling and
pagination, which we route through our own policy service anyway.

Django 6.x ships Content Security Policy support, so we do not add `django-csp`.

## Consequences

API permission checks are DRF permission classes that delegate to the policy service.
