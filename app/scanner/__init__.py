"""Phase 18a: the AWS security scanner — scan the account slice itself runs in.

A supervisor LangGraph (``app.scanner.graph``) fans out to four read-only boto3 checks
(``app.scanner.checks``: public S3, world-open security groups, unencrypted storage, IAM
risk), a collector merges the findings, and ``app.scanner.service`` persists them and
alerts on any *new* HIGH finding through the existing email/WhatsApp pipe. Cost Explorer
spend is pulled once a day (``app.scanner.cost``), latched in Redis because that API bills
per call. The ``/scanner/*`` endpoints (``app.scanner.routes``) expose runs and costs.

All of it is fire-and-forget — a detached background task, never the request path — and
every boto3 call fails open. boto3 is imported lazily (only when a scan runs), so importing
this package at startup, which ``app.main`` does, never pulls the SDK in.

Only the router is re-exported here (a light import). Everything else is imported from its
own module by the code that needs it, keeping this package import cheap.
"""

from __future__ import annotations

from app.scanner.routes import router

__all__ = ["router"]
