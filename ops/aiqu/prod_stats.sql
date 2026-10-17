-- OPS1: цифры для решений владельца, только чтение. Один запрос на строку.
SELECT 'reminders_failed_30d=' || (SELECT count(*) FROM scheduled_messages WHERE delivery_status = 'failed_permanent' AND created_at >= datetime('now', '-30 days'));
SELECT 'reminders_total_30d=' || (SELECT count(*) FROM scheduled_messages WHERE created_at >= datetime('now', '-30 days'));
SELECT 'subs_active=' || (SELECT count(*) FROM subscriptions WHERE is_disabled = 0);
SELECT 'users_with_subs=' || (SELECT count(DISTINCT user_id) FROM subscriptions WHERE is_disabled = 0);
SELECT 'channels_multi_tag_or_period=' || (SELECT count(*) FROM (SELECT user_id, channel_username FROM subscriptions WHERE is_disabled = 0 GROUP BY user_id, channel_username HAVING COUNT(DISTINCT COALESCE(tag, '')) > 1 OR COUNT(DISTINCT period) > 1));
