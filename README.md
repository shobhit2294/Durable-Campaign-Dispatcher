# Durable Campaign Dispatcher

A Python service that schedules outbound calls safely across campaign windows, tenant limits,
callbacks, retries, and global do-not-call requests. The dispatcher persists each call intent
before contacting the carrier and recovers ambiguous requests using an idempotency key.

## Project layout

```text
campaign_dispatcher/   service and scheduling rules
provider/              local carrier simulator and synthetic fixtures
tests/                 fast scheduling tests
docs/                  domain terms
```

## Run locally

Requires Python 3.11 or newer. From the repository root, create an environment and install the
dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Generate a Fernet key once, keep it private, and reuse the same key whenever you restart the
dispatcher:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
$env:DISPATCHER_FERNET_KEY = "<generated-key>"
```

Start the local carrier simulator in one terminal:

```powershell
cd provider
python -m mock_telephony --failure-scale 0
```

Open a second terminal at the repository root, set the same `DISPATCHER_FERNET_KEY`, and start
the service:

```powershell
$env:DISPATCHER_FERNET_KEY = "<the-same-generated-key>"
python -m campaign_dispatcher.dispatcher
```

The service listens on port 8200 and writes `final_state.csv` after all leads reach a final
state. `/status` reports current progress. By default, the simulator advances 10 simulated minutes
per real second. After a clean run, you can exercise the provider's failure injection by restarting
it with `python -m mock_telephony --failure-scale 1 --speed 1200 --seed 2026`.

Run the scheduling tests with:

```powershell
pytest
```

## Reliability notes

- SQLite stores the lead lifecycle and each stable provider request ID.
- Phone numbers are encrypted at rest; keyed fingerprints enforce phone-level exclusion and
  global DND without storing plaintext lookup keys.
- The provider's simulated clock controls every call window, retry, and callback decision.
- Signed webhooks are idempotent, and provider polling recovers results when webhooks are lost.

See [SUBMISSION.md](./SUBMISSION.md) for the state model, trade-offs, and verified run results.
