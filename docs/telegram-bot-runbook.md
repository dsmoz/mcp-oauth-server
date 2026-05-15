# Telegram Bot Runbook

Operational guide for the credit top-up approval flow over Telegram.

## Architecture

1. User clicks **Request top-up** in the portal → row inserted in `credit_topup_requests` (status=pending).
2. `send_topup_request_notice` posts a message to the owner chat with two inline buttons:
   - `✅ Approve` → `callback_data=topup_approve:<request_id>`
   - `❌ Reject`  → `callback_data=topup_reject:<request_id>`
3. Owner taps a button → Telegram POSTs to `https://connect.dsmozconsultancy.com/telegram/webhook` with header `X-Telegram-Bot-Api-Secret-Token`.
4. `telegram_webhook` route validates secret, dispatches to `_handle_topup_callback`, mutates `credit_topup_requests` + `users.credit_balance`, and calls Telegram `answerCallbackQuery` / `editMessageText` for visual confirmation.

The browser-facing detail page `GET /admin/topup-requests/{request_id}` is also available for reviewing a single request and shows the same Approve / Reject forms; it requires HTTP Basic admin auth.

## Configuration

All three values can be set in the admin settings UI (`/admin/settings`, "Telegram Bot" card). Database values override the env vars of the same name.

| Setting | DB key | Env fallback | Purpose |
|---|---|---|---|
| Bot Token | `telegram_bot_token` | `TELEGRAM_BOT_TOKEN` | Auth with Telegram Bot API |
| Owner Chat ID | `telegram_chat_id` | `TELEGRAM_OWNER_CHAT_ID` | Destination chat for notifications |
| Webhook Secret | `telegram_webhook_secret` | `TELEGRAM_WEBHOOK_SECRET` | Echoed by Telegram in `X-Telegram-Bot-Api-Secret-Token` so the server can verify requests |

Both the send-side (`register_webhook`) and the receive-side (`telegram_webhook`) resolve the webhook secret via the same `_webhook_secret()` helper, so DB and env can never disagree.

## After changing any setting

You **must** re-register the webhook so Telegram stores the new secret / URL:

- Admin UI: settings card → **Re-register Webhook** button.
- API: `POST /admin/telegram/re-register-webhook` (admin auth).

The endpoint deletes the old registration, calls `setWebhook` with the current settings, and returns:

```json
{
  "issuer_url": "https://connect.dsmozconsultancy.com",
  "webhook_url": "https://connect.dsmozconsultancy.com/telegram/webhook",
  "deleted":   { ...Telegram response... },
  "set_result": { "ok": true, "description": "Webhook was set" },
  "webhook_info": { ...current Telegram webhook state... }
}
```

If `set_result.ok` is false, the response contains Telegram's error description and a Sentry `error` event is captured.

## Diagnosing approval failures

Symptom: tapping `✅ Approve` shows no toast and the request stays `pending`.

1. **Verify Telegram has a webhook URL.**
   ```
   GET /admin/telegram/webhook-info
   ```
   If `url` is empty → click **Re-register Webhook**. If `last_error_message` is set → fix the underlying issue (TLS, 5xx, secret mismatch) and re-register.

2. **Check Sentry** (project `mcp-auth-server`).
   - `telegram_webhook: secret mismatch (incoming_len=X, expected_len=Y)` → Telegram's stored secret doesn't match the current `_webhook_secret()` value. Re-register the webhook.
   - Exception from `_do_approve_topup` / `_do_reject_topup` → DB error; check Supabase. The request row or user row may be missing.

3. **Manually probe the webhook** (no admin auth required, the route is secret-protected):
   ```bash
   curl -i -X POST https://connect.dsmozconsultancy.com/telegram/webhook \
     -H "Content-Type: application/json" \
     -H "X-Telegram-Bot-Api-Secret-Token: <SECRET>" \
     -d '{"update_id":1,"callback_query":{"id":"x","data":"topup_approve:<request_id>","message":{"chat":{"id":1},"message_id":1}}}'
   ```
   - `200 {"ok":true}` → handler ran (check Sentry for downstream errors).
   - `403 {"ok":false,"reason":"secret mismatch"}` → secret wrong.
   - `404 / 405 / cloudflare error` → routing problem before the app.

## Known root causes (2026-05-15)

- **Missing GET detail route**: the Telegram message used to include a `Review:` URL that linked to `/admin/topup-requests/{id}`, but only the list route existed; clicking the link returned `{"detail":"Not Found"}`. Fixed by adding the GET detail route. The URL was later dropped from the notification message — the inline buttons are the canonical approval path; the detail page is still reachable from the list.
- **Webhook URL emptied by partial re-register**: `register_webhook` swallowed Telegram's `setWebhook` errors with a stderr print, so a failed call left Telegram with `url=""` and no diagnostic surface. `register_webhook` now returns Telegram's response and captures Sentry on failure; the admin re-register endpoint exposes that response so the UI can show why setWebhook failed.
- **Send/receive secret divergence**: the receive path read `settings.TELEGRAM_WEBHOOK_SECRET` directly (env only) while the send/register paths used the DB-overridable resolver. Unified both on `_webhook_secret()`.
