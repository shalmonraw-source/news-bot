// MER5 · אישור מיידי: Telegram webhook -> Cloudflare Worker -> GitHub Actions
// משתני סביבה (Secrets) שצריך להגדיר ב-Cloudflare:
//   BOT_TOKEN       הטוקן של בוט הטלגרם
//   ALLOWED_CHAT_ID המזהה שלך בטלגרם (רק ממנו מתקבלות החלטות)
//   GH_TOKEN        טוקן GitHub (fine-grained) עם Actions: read & write על הריפו
//   GH_REPO         לדוגמה: shalmonraw-source/news-bot
//   WEBHOOK_SECRET  מחרוזת אקראית ארוכה, זהה לזו שמוגדרת ב-setWebhook

async function tg(env, method, payload) {
  const r = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return r.json();
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("MER5 ok");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const update = await request.json();
    const cq = update.callback_query;
    if (!cq) return new Response("ok");
    if (String(cq.from.id) !== String(env.ALLOWED_CHAT_ID)) {
      await tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "אין הרשאה" });
      return new Response("ok");
    }
    const data = cq.data || "";
    const [action] = data.split(":");
    if (action !== "a" && action !== "r") return new Response("ok");
    const approve = action === "a";

    // 1) תגובה מיידית בטלגרם
    await tg(env, "answerCallbackQuery", {
      callback_query_id: cq.id,
      text: approve ? "אושר ✅ מתפרסם..." : "נדחה ❌",
    });
    const msg = cq.message || {};
    if (msg.message_id) {
      await tg(env, "editMessageText", {
        chat_id: msg.chat.id,
        message_id: msg.message_id,
        text: ((msg.text || "") + "\n\n— " + (approve ? "אושר ✅ מתפרסם עכשיו" : "נדחה ❌")).slice(0, 4000),
        link_preview_options: { is_disabled: true },
      });
    }

    // 2) הפעלת הבוט ב-GitHub עם ההחלטה
    const gh = await fetch(`https://api.github.com/repos/${env.GH_REPO}/actions/workflows/bot.yml/dispatches`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.GH_TOKEN}`,
        accept: "application/vnd.github+json",
        "user-agent": "mer5-worker",
        "x-github-api-version": "2022-11-28",
      },
      body: JSON.stringify({ ref: "main", inputs: { mode: "decide", decision: data } }),
    });
    if (!gh.ok) {
      await tg(env, "sendMessage", { chat_id: msg.chat.id, text: `⚠️ ההחלטה נרשמה בטלגרם אך GitHub החזיר ${gh.status}. בדוק את GH_TOKEN.` });
    }
    return new Response("ok");
  },
};
