import os, re, logging, requests, dns.resolver, concurrent.futures
from bs4 import BeautifulSoup
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import threading

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = re.sub(r'\s+', '', os.environ.get('TELEGRAM_TOKEN', ''))
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN missing!")

flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Email Finder Bot 📧"
def run_flask(): flask_app.run(host='0.0.0.0', port=8080)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

# ── Pattern generator ─────────────────────────────────────────────────
def generate_patterns(first, last, domain):
    f, l = first.lower(), last.lower()
    fi, li = f[0], l[0]
    return [
        f"{f}.{l}@{domain}",
        f"{f}{l}@{domain}",
        f"{fi}{l}@{domain}",
        f"{fi}.{l}@{domain}",
        f"{f}@{domain}",
        f"{f}{li}@{domain}",
        f"{l}.{f}@{domain}",
        f"{l}{fi}@{domain}",
        f"{f}_{l}@{domain}",
    ]

# ── MX check ──────────────────────────────────────────────────────────
def get_mx(domain):
    try:
        records = dns.resolver.resolve(domain, 'MX')
        return str(min(records, key=lambda r: r.preference).exchange).rstrip('.')
    except:
        return None

# ── Disify free verify (fast, no SMTP needed) ─────────────────────────
def disify_check(email):
    """Free email check — returns (format_ok, dns_ok, disposable)"""
    try:
        r = requests.get(
            f'https://www.disify.com/api/email/{email}',
            timeout=5, headers=HEADERS
        )
        if r.status_code == 200:
            d = r.json()
            return d.get('format', False), d.get('dns', False), d.get('disposable', False)
    except:
        pass
    return False, False, False

# ── Verify multiple patterns IN PARALLEL ─────────────────────────────
def verify_patterns_parallel(patterns):
    """Check all patterns at once using threads — much faster."""
    results = []

    def check(email):
        fmt, dns_ok, disposable = disify_check(email)
        return email, fmt, dns_ok, disposable

    with concurrent.futures.ThreadPoolExecutor(max_workers=9) as ex:
        futures = {ex.submit(check, p): p for p in patterns}
        for future in concurrent.futures.as_completed(futures, timeout=12):
            try:
                email, fmt, dns_ok, disposable = future.result()
                if fmt and dns_ok and not disposable:
                    results.append(email)
            except:
                pass

    return results

# ── Google search for emails ──────────────────────────────────────────
def google_search(query):
    try:
        r = requests.get(
            f'https://www.google.com/search?q={requests.utils.quote(query)}&num=10',
            headers=HEADERS, timeout=8
        )
        return re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', r.text)
    except:
        return []

# ── Scan domain for email pattern ────────────────────────────────────
def scan_domain(domain):
    """Fast domain scan — find email pattern in <5 seconds."""
    emails = []

    def fetch(url):
        try:
            r = requests.get(url, headers=HEADERS, timeout=5)
            found = re.findall(r'[a-zA-Z0-9._%+\-]+@' + re.escape(domain), r.text)
            emails.extend(found)
        except:
            pass

    pages = [
        f'https://{domain}',
        f'https://{domain}/contact',
        f'https://{domain}/about',
        f'https://{domain}/team',
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        ex.map(fetch, pages)

    # Also Google search
    google = google_search(f'@{domain} email contact')
    emails.extend(google)

    emails = list(set(e.lower() for e in emails if domain in e))

    if not emails:
        return 'unknown', []

    # Detect pattern
    patterns = []
    for email in emails:
        local = email.split('@')[0]
        if re.match(r'^[a-z]+\.[a-z]+$', local): patterns.append('firstname.lastname')
        elif re.match(r'^[a-z]\.[a-z]+$', local): patterns.append('f.lastname')
        elif re.match(r'^[a-z][a-z]{4,}$', local): patterns.append('firstnamelastname')
        elif re.match(r'^[a-z][a-z]+$', local): patterns.append('flastname')

    best = max(set(patterns), key=patterns.count) if patterns else 'unknown'
    return best, emails[:8]

# ── MAIN FIND — fast version ──────────────────────────────────────────
def find_email_fast(first, last, domain):
    results = []

    # Step 1: Google search (instant if email is public)
    google_hits = google_search(f'"{first} {last}" "@{domain}"')
    clean = [e for e in google_hits if domain in e]
    if clean:
        return [{'email': clean[0], 'confidence': 95, 'method': '🌐 Found publicly online'}]

    # Step 2: Find domain pattern (parallel, ~5 seconds)
    pattern, known = scan_domain(domain)

    # Step 3: Build priority patterns based on known pattern
    all_patterns = generate_patterns(first, last, domain)
    if pattern == 'firstname.lastname':
        priority = f"{first.lower()}.{last.lower()}@{domain}"
        all_patterns = [priority] + [p for p in all_patterns if p != priority]
    elif pattern == 'f.lastname':
        priority = f"{first[0].lower()}.{last.lower()}@{domain}"
        all_patterns = [priority] + [p for p in all_patterns if p != priority]
    elif pattern == 'firstnamelastname':
        priority = f"{first.lower()}{last.lower()}@{domain}"
        all_patterns = [priority] + [p for p in all_patterns if p != priority]
    elif pattern == 'flastname':
        priority = f"{first[0].lower()}{last.lower()}@{domain}"
        all_patterns = [priority] + [p for p in all_patterns if p != priority]

    # Step 4: Verify all patterns IN PARALLEL (~8 seconds total)
    verified = verify_patterns_parallel(all_patterns)

    if verified:
        conf = 90 if pattern != 'unknown' else 72
        method = f"✅ Pattern: `{pattern}`" if pattern != 'unknown' else "✅ DNS verified"
        return [{'email': verified[0], 'confidence': conf, 'method': method}]

    # Step 5: Return best guess even without full verification
    if pattern != 'unknown' and all_patterns:
        return [{'email': all_patterns[0], 'confidence': 55, 'method': f"📐 Best guess ({pattern})"}]

    return []

# ── Telegram handlers ─────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📧 *Email Finder Bot*\n\n"
        "*Commands:*\n"
        "`/find John Doe apple.com`\n"
        "→ Find work email\n\n"
        "`/domain tesla.com`\n"
        "→ Email pattern + known emails\n\n"
        "`/verify john@apple.com`\n"
        "→ Check if email is real\n\n"
        "Paste a LinkedIn URL — auto detected.\n\n"
        "⚡ Results in ~10 seconds",
        parse_mode='Markdown')

async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or len(ctx.args) < 3:
        await update.message.reply_text(
            "Usage: `/find FirstName LastName domain.com`\n"
            "Example: `/find John Doe apple.com`",
            parse_mode='Markdown')
        return

    domain = ctx.args[-1].lower().replace('www.','').replace('https://','').replace('http://','')
    first  = ctx.args[0].capitalize()
    last   = ' '.join(ctx.args[1:-1]).capitalize()

    await update.message.reply_text(
        f"🔍 Finding *{first} {last}* at *{domain}*...\n⚡ ~10 seconds",
        parse_mode='Markdown')

    def search():
        return find_email_fast(first, last, domain)

    with concurrent.futures.ThreadPoolExecutor() as ex:
        future = ex.submit(search)
        try:
            results = future.result(timeout=30)
        except concurrent.futures.TimeoutError:
            results = []

    if not results:
        await update.message.reply_text(
            f"❌ No email found for *{first} {last}* at *{domain}*\n\n"
            f"Try `/domain {domain}` to see their email format.",
            parse_mode='Markdown')
        return

    r = results[0]
    conf = r['confidence']
    emoji = "🟢" if conf >= 85 else "🟡" if conf >= 60 else "🔴"
    await update.message.reply_text(
        f"📧 *Email Found*\n\n"
        f"`{r['email']}`\n\n"
        f"Confidence: {emoji} {conf}%\n"
        f"Source: {r['method']}",
        parse_mode='Markdown')

async def cmd_domain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/domain apple.com`", parse_mode='Markdown')
        return

    domain = ctx.args[0].lower().replace('www.','').replace('https://','').replace('http://','')
    await update.message.reply_text(f"🔍 Scanning *{domain}*...", parse_mode='Markdown')

    mx = get_mx(domain)
    if not mx:
        await update.message.reply_text(f"❌ No mail server for *{domain}*", parse_mode='Markdown')
        return

    pattern, emails = scan_domain(domain)

    lines = [f"📧 *{domain}*\n", f"📐 Pattern: `{pattern}`\n"]
    if emails:
        lines.append("*Known emails:*")
        for e in emails[:8]:
            lines.append(f"• `{e}`")
    else:
        lines.append("No public emails found.")

    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

async def cmd_verify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/verify email@domain.com`", parse_mode='Markdown')
        return

    email = ctx.args[0].lower().strip()
    await update.message.reply_text(f"🔍 Verifying `{email}`...", parse_mode='Markdown')

    fmt, dns_ok, disposable = disify_check(email)
    mx = get_mx(email.split('@')[1])

    if fmt and dns_ok and not disposable:
        status, emoji, conf = "LIKELY VALID", "🟢", 85
    elif fmt and dns_ok:
        status, emoji, conf = "VALID FORMAT", "🟡", 60
    elif not fmt:
        status, emoji, conf = "INVALID FORMAT", "🔴", 0
    else:
        status, emoji, conf = "UNKNOWN", "🟡", 40

    await update.message.reply_text(
        f"📧 `{email}`\n\n"
        f"Status: {emoji} *{status}*\n"
        f"Confidence: {conf}%\n"
        f"DNS: {'✅' if dns_ok else '❌'}\n"
        f"Mail server: {'✅ ' + (mx[:35] if mx else '') if mx else '❌ None'}\n"
        f"Disposable: {'⚠️ Yes' if disposable else '✅ No'}",
        parse_mode='Markdown')

async def handle_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or '').strip()
    if 'linkedin.com/in/' in text:
        await update.message.reply_text(
            "Got the LinkedIn URL.\n\n"
            "Find their name + company from the profile, then:\n"
            "`/find FirstName LastName company.com`",
            parse_mode='Markdown')
        return
    if re.match(r'^[\w.+\-]+@[\w\-]+\.[a-z]{2,}$', text):
        ctx.args = [text]
        await cmd_verify(update, ctx)
        return
    await update.message.reply_text(
        "Commands:\n"
        "`/find John Doe company.com`\n"
        "`/domain company.com`\n"
        "`/verify email@co.com`",
        parse_mode='Markdown')

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, fn in [('start', cmd_start), ('find', cmd_find),
                    ('domain', cmd_domain), ('verify', cmd_verify)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    logger.info("📧 Email Bot live")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
