# Durable Campaign Dispatcher

This project runs outbound calling campaigns through a local telephony provider. It decides when a
lead can be called, keeps each tenant within its call limit, waits before retries, handles requested
callbacks, and blocks numbers that ask not to be called again.

The important part is what happens when something goes wrong. Every call is saved in SQLite before
the provider is contacted. That saved record contains a stable request ID, so a restart can check
what happened and safely continue without placing the same call twice.

## Project folders

```text
campaign_dispatcher/   The dispatcher and scheduling code
provider/              The local telephony simulator and fixtures
tests/                 Unit tests for the scheduling rules
docs/                  Project terminology
```

## Start the project

Use Python 3.11 or newer. Run these commands from the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The phone numbers are encrypted in the SQLite database. Create a Fernet key once and use the same
key every time the dispatcher is restarted:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
$env:DISPATCHER_FERNET_KEY = "<generated-key>"
```

Open one terminal and start the provider:

```powershell
cd provider
python -m mock_telephony --failure-scale 0
```

Open a second terminal at the repository root, set the same key, and start the dispatcher:

```powershell
$env:DISPATCHER_FERNET_KEY = "<the-same-generated-key>"
python -m campaign_dispatcher.dispatcher
```

The dispatcher listens on port `8200`. When it finishes, it writes `final_state.csv`. While it is
running, `GET http://127.0.0.1:8200/status` shows progress. The provider's clock runs faster than
real time: by default, one real second represents ten simulated minutes.

After a clean run, you can try the provider's failure cases:

```powershell
python -m mock_telephony --failure-scale 1 --speed 1200 --seed 2026
```

Run the tests from the repository root:

```powershell
pytest
```

## Reliability basics

- SQLite stores the lead state, call state, and provider request IDs.
- Phone numbers are encrypted. A keyed fingerprint is used when the service needs to compare phone
  numbers for active-call and DND checks.
- The provider's simulated time is used for calling windows, callbacks, and retries.
- Webhooks are checked with HMAC and recorded only once. If a webhook is lost, the dispatcher polls
  the provider for the result.

For the design explanation, state diagram, failure cases, and run results, see
[SUBMISSION.md](./SUBMISSION.md).
