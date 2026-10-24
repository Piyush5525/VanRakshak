# Notifications and dispatch

## Incident lifecycle
`DETECTED -> PROCESSING -> NEEDS_REVIEW -> APPROVED_FOR_INSPECTION -> ASSIGNED -> DISPATCHED -> ACKNOWLEDGED -> FIELD_VERIFICATION_PENDING -> CONFIRMED | UNCONFIRMED -> CLOSED` (`DISMISSED` from early states). Transitions are enforced server-side (`domain/state_machine.py`) and every change is audited. Review needs reviewer/admin; assign and dispatch need officer/admin. A model run only ever creates `NEEDS_REVIEW` cases; it can never dispatch.

## Delivery status ladder
`CAPTURED_NOT_SENT` (nothing left the system) -> `SENT_TO_PROVIDER` (provider accepted; not proof of delivery or reading) -> `DELIVERED` (voice only, from a signed provider status webhook) -> `ACKNOWLEDGED` (an authenticated human pressed acknowledge) | `FAILED`. A failed provider call returns 502, leaves the incident `ASSIGNED`, and never produces an acknowledgement.

## Safe by default
Real sends require **all** of: `ENVIRONMENT=production`, `ALLOW_REAL_NOTIFICATIONS=true`, provider credentials, and an incident that passed review and assignment. Otherwise messages are captured and stored. Startup refuses `ALLOW_REAL_NOTIFICATIONS` without `CALLBACK_SECRET`. Tests run in `ENVIRONMENT=test` with HTTP transports replaced; no real request is made.

## Idempotency
Key = SHA-256(incident, channel, recipient, event) with a unique index; a repeated dispatch returns the existing notification with `duplicate_suppressed: true`.

## Telegram
Bot API `sendMessage` with an "Acknowledge" inline button. Webhook `POST /notifications/telegram/callback` requires the `X-Telegram-Bot-Api-Secret-Token` header to equal `CALLBACK_SECRET` **and** the pressing user or chat to equal the notification's recipient; forged or forwarded callbacks get 403. You must register the webhook yourself with Telegram (`setWebhook` with `secret_token`) and expose a public HTTPS URL.

## Voice (Twilio)
Calls are created through the Twilio REST API with a spoken summary and "press 1 to acknowledge". Status and DTMF webhooks verify `X-Twilio-Signature` (HMAC-SHA1) against the auth token. Needs `VOICE_PROVIDER=twilio`, account SID, auth token, from-number and `PUBLIC_BASE_URL`; paid usage applies. Not exercised against the real Twilio service in this build.

## Routing
`ROUTING_PROVIDER` is an ordered list, default `mapbox,serpapi` (also `osrm`). Mapbox Directions gives distance, duration, full geometry and alternatives. If it fails, SerpAPI's Google Maps directions supplies driving distance and time only (no geometry) and the response says so and notes the fallback. A warning is added when a waypoint is more than 500 m from a road. Every response states that closures, seasonal access, permits and safety are **not verified**. If all providers fail the status is `error` with provider names, never a guessed route. Patrol planning orders stops greedily by nearest neighbour and routes each leg; it is not an optimal tour. Origin defaults to the nearest registered station by straight line (availability unknown). Stations are stored with contacts masked in API responses.
