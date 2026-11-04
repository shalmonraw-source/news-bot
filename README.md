# news-bot

בוט חדשות תעופה, שלב א' (בלי פייסבוק).

**זרימה:** כל 10 דקות ב-GitHub Actions → שאיבת RSS (`sources.json`) → Claude בוחר ומנסח בעברית → הצעת פוסט בטלגרם עם כפתורי אשר/דחה → פוסטים שאושרו נשמרים ב-`state.json` (`approved`).

**סודות נדרשים (Settings → Secrets → Actions):** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`.

**הרצה ידנית:** לשונית Actions → bot → Run workflow (mode: `test` לבדיקת חיבור, `run` לריצה רגילה).

**הגבלות:** עד 4 הצעות לריצה, 8 ביום (`DAILY_CAP`). לחיצות על כפתורים מעובדות בריצה הבאה (עד 10 דקות).

**שלב ב' (בהמשך):** פרסום הפוסטים המאושרים לעמוד פייסבוק דרך Graph API, תמונות מתבנית, וזיהוי אירועים מ-Flightradar24.
