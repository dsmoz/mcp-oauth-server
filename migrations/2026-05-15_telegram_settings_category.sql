-- Move Telegram settings into their own admin category and add webhook secret.
-- Idempotent — safe to re-run.

UPDATE admin_settings
   SET category    = 'telegram',
       label       = 'Bot Token',
       description = 'From @BotFather on Telegram'
 WHERE key = 'telegram_bot_token';

UPDATE admin_settings
   SET category    = 'telegram',
       label       = 'Owner Chat ID',
       description = 'Your personal chat ID — get from @userinfobot'
 WHERE key = 'telegram_chat_id';

INSERT INTO admin_settings (key, value, category, label, description, value_type)
VALUES (
  'telegram_webhook_secret',
  '',
  'telegram',
  'Webhook Secret',
  'Random string sent in x-telegram-bot-api-secret-token header to verify incoming updates',
  'secret'
)
ON CONFLICT (key) DO NOTHING;
