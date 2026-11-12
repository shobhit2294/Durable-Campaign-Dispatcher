# AI Usage

## Tools used

ChatGPT (Codex).

## What it helped with

Codex read the project brief and provider behavior, then drafted much of the dispatcher,
scheduling helpers, tests, and project documentation. It also helped run the tests and a clean
provider integration run.

## What I need to review myself

I need to read through the code and verify that I can explain the request-ID recovery flow,
window and retry calculations, DND precedence, and encrypted phone storage before sharing this
project. The design choices currently used are documented in `SUBMISSION.md`; I should change
them if they do not reflect my own understanding.

## Example of an AI mistake and how it was caught

The first timezone implementation used `ZoneInfo("Asia/Kolkata")`. On this Windows Python
environment the IANA timezone database was missing, and the test run failed during import. The
code now uses India’s fixed UTC+05:30 offset, which matches the provider clock and does not
require a timezone database.

## Verification

Five scheduling tests passed. A clean local provider run completed 200 leads with zero provider
violations and no differences from `/admin/leads`. A second run used failure injection, was
force-stopped after 92 calls, and resumed from the same database; it finished with zero
violations and no differences from `/admin/leads`. The provider reported 40 duplicate and 10
lost webhooks; polling recovered the missing results.
