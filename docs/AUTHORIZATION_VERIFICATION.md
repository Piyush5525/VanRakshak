# Authorization-record verification

**This is decision support. A missing or matching record is never a legal finding.** Only a competent authority can determine whether an activity is lawful.

## What exists
* `POST /authorizations/ingest`: store a record (authority, project ID/name, status, geometry, dates, source URL, evidence) entered by a reviewer from an official source or uploaded document.
* `GET /authorizations/search`, `POST /authorizations/match-assessment`.
* Matching (`geoanalysis.assess_candidate`): overlap of the change polygon with the record geometry (ellipsoidal areas), validity window against the comparison date, and record status. Output states: `NO_RECORD_FOUND`, `POTENTIAL_MATCH`, `MATCH_REQUIRES_REVIEW`, `AUTHORIZATION_STATUS_UNCLEAR`, `POSSIBLE_SCOPE_OR_CONDITION_MISMATCH` (overlap < 95%), `RECORD_EXPIRED_OR_REVOKED_CANDIDATE`, `INSUFFICIENT_EVIDENCE` (no overlap). Application, recommendation, issued, expired and rejected remain distinct statuses; pre-decision records never count as authorisation.
* Every assessment states the search coverage ("local ingested records only") and carries the warning that absence of a record is not a finding of illegality.

## What does not exist
* **No automatic PARIVESH integration.** PARIVESH (https://parivesh.nic.in/) is the official portal, but this build has no documented public API integration and does not scrape it or bypass CAPTCHAs or access controls. Records must be entered from the official source or imported from official bulk data, and each should cite its URL and retrieval date.
* **Legal framework not encoded.** The Van (Sanrakshan Evam Samvardhan) Adhiniyam 1980 as amended, wildlife and state rules are not modelled as a decision tree. The application deliberately does not claim to apply them. These instruments should be verified on India Code (https://www.indiacode.nic.in/) and MoEFCC notifications by a legal reviewer; **this was not done in this build and no "date checked" is claimed.**
* Project-condition interpretation (e.g. permitted area, felling limits) is manual.

## Suspected-unauthorised cases
A candidate with `NO_RECORD_FOUND` becomes a review case, not an allegation. Reports show the evidence, the search coverage, model confidence and limitations, and the audit trail. Nothing is published automatically.
