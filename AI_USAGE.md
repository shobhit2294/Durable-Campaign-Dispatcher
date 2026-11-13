# AI usage

## How I used Codex

I used ChatGPT (Codex) while building this project. It helped me read the provider brief and inspect
the simulator, sketch the dispatcher and scheduling helpers, write the initial tests, and improve the
project documentation. It also helped me run the unit tests and a clean provider integration run.

## What I reviewed myself

I still need to read the implementation end to end before sharing it and be able to explain the
request-ID recovery flow, window and retry calculations, DND precedence, and encrypted phone storage.
The design decisions are recorded in [SUBMISSION.md](./SUBMISSION.md); I will change that document
if it stops matching the code.

## An AI mistake I caught

The first timezone version used `ZoneInfo("Asia/Kolkata")`. The Windows environment did not have an
IANA timezone database, so imports failed during the test run. I changed the scheduling code to use
India's fixed UTC+05:30 offset, which matches the provider clock and does not require that database.

## Verification

Five scheduling tests passed. A clean provider run completed 200 leads with zero provider violations
and no differences from `/admin/leads`. A second run used failure injection, stopped the dispatcher
after 92 calls, and resumed it with the same database. It also finished with zero violations and no
differences from `/admin/leads`. The provider reported 40 duplicate webhook deliveries and 10 lost
deliveries; polling recovered the missing results.
