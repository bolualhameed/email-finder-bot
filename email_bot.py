import os, re, logging, requests, socket, smtplib, dns.resolver
from itertools import product
from bs4 import BeautifulSoup
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import threading, time, asyncio

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = re.sub(r'\s+', '', os.environ.get('TELEGRAM_TOKEN', ''))
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN missing!")

# ── Flask keep-alive ──────────────────────────────────────────────────
flask_app = Flask(__name__)
@flask_app.route('/')
def home(): return "Email Finder Bot 📧"
def run_flask(): flask_app.run(host='0.0.0.0', port=8080)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

# ── 1. EMAIL PATTERN GENERATOR ────────────────────────────────────────
def generate_patterns(first, last, domain):
    """Generate all common corporate email patterns."""
    f  = first.lower().strip()
    l  = last.lower().strip()
    fi = f[0]   # first initial
    li = l[0]   # last initial
    return [
        f"{f}@{domain}",
        f"{l}@{domain}",
        f"{f}{l}@{domain}",
        f"{f}.{l}@{domain}",
        f"{f}_{l}@{domain}",
        f"{fi}{l}@{domain}",
        f"{fi}.{l}@{domain}",
        f"{f}{li}@{domain}",
        f"{l}{f}@{domain}",
        f"{l}.{f}@{domain}",
        f"{l}{fi}@{domain}",
        f"{f}-{l}@{domain}",
        f"{fi}{li}@{domain}",
    ]

# ── 2. MX RECORD CHECK ───────────────────────────────────────────────
def get_mx(domain):
    """Get mail server for domain."""
    try:
        records = dns.resolver.resolve(domain, 'MX')
        return str(min(records, key=lambda r: r.preference).exchange).rstrip('.')
    except Exception:
        return None

def domain_has_email(domain):
    """Check if domain accepts emails at all."""
    return get_mx(domain) is not None

# ── 3. SMTP VERIFICATION ─────────────────────────────────────────────
def smtp_verify(email, timeout=6):
    """
    Verify email exists via SMTP RCPT TO without sending.
    Returns: True (exists), False (doesn't exist), None (inconclusive)
    """
    domain = email.split('@')[1]
    mx = get_mx(domain)
    if not mx:
        return False

    try:
        with smtplib.SMTP(timeout=timeout) as s:
            s.connect(mx, 25)
            s.ehlo('outreach.com')
            s.mail('noreply@outreach.com')
            code, _ = s.rcpt(str(email))
            s.quit()
            return code == 250
    except smtplib.SMTPServerDisconnected:
        return None
    except smtplib.SMTPConnectError:
        return None   # Server unreachable (port 25 blocked on Render)
    except smtplib.SMTPResponseException as e:
        return e.smtp_code == 250
    except Exception as e:
        logger.debug(f"SMTP error for {email}: {e}")
        return None

# ── 4. FREE EMAIL VERIFIER (disify.com — no key needed) ───────────────
def disify_verify(email):
    """Free unlimited email check — no API key."""
    try:
        r = requests.get(
            f'https://www.disify.com/api/email/{email}',
            timeout=8, headers=HEADERS
        )
        if r.status_code == 200:
            d = r.json()
            return {
                'format_ok': d.get('format', False),
                'dns_ok':    d.get('dns', False),
                'disposable': d.get('disposable', False),
            }
    except: pass
    return None

# ── 5. GOOGLE SEARCH FOR EMAILS ───────────────────────────────────────
def google_find_email(name, domain):
    """Search Google for publicly listed email."""
    query = f'"{name}" "@{domain}"'
    try:
        r = requests.get(
            f'https://www.google.com/search?q={requests.utils.quote(query)}&num=20',
            headers=HEADERS, timeout=10
        )
        found = re.findall(
            r'[a-zA-Z0-9._%+\-]+@' + re.escape(domain),
            r.text
        )
        return list(set(found))
    except Exception as e:
        logger.error(f"Google search: {e}")
        return []

# ── 6. FIND DOMAIN EMAIL PATTERN ──────────────────────────────────────
def find_domain_pattern(domain):
    """
    Find which email pattern a company uses by scanning:
    - Their website contact/about pages
    - Google search results
    """
    found_emails = []

    # Scan website
    for path in ['', '/contact', '/about', '/team', '/contact-us']:
        try:
            r = requests.get(f'https://{domain}{path}', headers=HEADERS, timeout=8)
            emails = re.findall(r'[a-zA-Z0-9._%+\-]+@' + re.escape(domain), r.text)
            found_emails.extend(emails)
        except: continue

    # Google search for emails at domain
    try:
        r = requests.get(
            f'https://www.google.com/search?q=%40{domain}&num=20',
            headers=HEADERS, timeout=10
        )
        emails = re.findall(r'[a-zA-Z0-9._%+\-]+@' + re.escape(domain), r.text)
        found_emails.extend(emails)
    except: pass

    found_emails = list(set(found_emails))

    if not found_emails:
        return None, []

    # Detect pattern from found emails
    patterns_seen = []
    for email in found_emails:
        local = email.split('@')[0]
        if re.match(r'^[a-z]+\.[a-z]+$', local):
            patterns_seen.append('firstname.lastname')
        elif re.match(r'^[a-z]+[a-z]+$', local) and len(local) > 6:
            patterns_seen.append('firstnamelastname')
        elif re.match(r'^[a-z]\.[a-z]+$', local):
            patterns_seen.append('f.lastname')
        elif re.match(r'^[a-z][a-z]+$', local) and len(local) <= 7:
            patterns_seen.append('flastname')

    pattern = max(set(patterns_seen), key=patterns_seen.count) if patterns_seen else 'unknown'
    return pattern, found_emails[:10]

# ── 7. SCRAPE LINKEDIN PUBLIC PROFILE ────────────────────────────────
def scrape_linkedin(url):
    """Scrape public LinkedIn profile for name + company."""
    try:
        # Use Google cache to avoid LinkedIn blocks
        cached = f"https://webcache.googleusercontent.com/search?q=cache:{url}"
        r = requests.get(cached, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')

        name = None
        company = None
        email = None

        # Find name
        name_tag = soup.find('h1')
        if name_tag:
            name = name_tag.get_text(strip=True)

        # Find company
        for tag in soup.find_all(['span', 'div', 'p']):
            text = tag.get_text(strip=True)
            if 'at ' in text.lower() and len(text) < 100:
                company = text
                break

        # Find any email in page
        emails_found = re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', r.text)
        if emails_found:
            email = emails_found[0]

        return {'name': name, 'company': company, 'email': email}
    except Exception as e:
        logger.error(f"LinkedIn scrape: {e}")
        return None

# ── MAIN FIND FUNCTION ────────────────────────────────────────────────
def find_email(first, last, domain):
    """
    Full pipeline:
    1. Check Google for existing email
    2. Generate patterns
    3. Verify each with disify + SMTP
    """
    results = []
    name = f"{first} {last}"

    # Step 1: Google search first (fastest)
    google_hits = google_find_email(name, domain)
    for email in google_hits:
        results.append({'email': email, 'confidence': 95, 'method': 'Found online'})

    if results:
        return results

    # Step 2: Find domain pattern to narrow down
    pattern, known_emails = find_domain_pattern(domain)

    # If we found real emails for this domain, use that pattern to guess this person's
    if pattern == 'firstname.lastname':
        priority = [f"{first.lower()}.{last.lower()}@{domain}"]
    elif pattern == 'firstnamelastname':
        priority = [f"{first.lower()}{last.lower()}@{domain}"]
    elif pattern == 'f.lastname':
        priority = [f"{first[0].lower()}.{last.lower()}@{domain}"]
    elif pattern == 'flastname':
        priority = [f"{first[0].lower()}{last.lower()}@{domain}"]
    else:
        priority = []

    # Step 3: Generate all patterns
    all_patterns = priority + [p for p in generate_patterns(first, last, domain) if p not in priority]

    # Step 4: Verify each
    for email in all_patterns:
        # Quick disify check first
        d = disify_verify(email)
        if d:
            if not d['format_ok'] or not d['dns_ok']:
                continue
            if d['disposable']:
                continue

        # SMTP check
        smtp_result = smtp_verify(email)
        if smtp_result is True:
            results.append({'email': email, 'confidence': 92, 'method': 'SMTP verified'})
            break  # Found it
        elif smtp_result is None:
            # Inconclusive — add as possible
            if pattern != 'unknown' and email in priority:
                results.append({'email': email, 'confidence': 70, 'method': f'Pattern match ({pattern})'})

    return results

# ── TELEGRAM HANDLERS ─────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📧 *Email Finder Bot — 100% Free & Unlimited*\n\n"
        "*No API keys. No limits.*\n\n"
        "Uses: Pattern generation + SMTP verification\n"
        "+ Google search + Domain scanning\n\n"
        "*Commands:*\n"
        "`/find John Doe apple.com`\n"
        "→ Find their work email\n\n"
        "`/domain tesla.com`\n"
        "→ Find email pattern + known emails\n\n"
        "`/verify john@apple.com`\n"
        "→ Check if email is real\n\n"
        "`/linkedin linkedin.com/in/johndoe`\n"
        "→ Extract info from LinkedIn\n\n"
        "Paste a LinkedIn URL directly — auto detected.",
        parse_mode='Markdown')

async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or len(ctx.args) < 3:
        await update.message.reply_text(
            "Usage: `/find FirstName LastName domain.com`\n\n"
            "Example: `/find John Doe apple.com`",
            parse_mode='Markdown')
        return

    domain = ctx.args[-1].lower().replace('www.','').replace('https://','').replace('http://','')
    first  = ctx.args[0].capitalize()
    last   = ' '.join(ctx.args[1:-1]).capitalize()

    await update.message.reply_text(
        f"🔍 Searching for *{first} {last}* at *{domain}*...\n"
        f"⏳ Checking patterns + verifying...",
        parse_mode='Markdown')

    # Run in thread to not block bot
    def search():
        return find_email(first, last, domain)

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as ex:
        future = ex.submit(search)
        results = future.result(timeout=45)

    if not results:
        await update.message.reply_text(
            f"❌ No email found for *{first} {last}* at *{domain}*\n\n"
            f"Try `/domain {domain}` to see the email pattern used.",
            parse_mode='Markdown')
        return

    for r in results[:3]:
        conf = r['confidence']
        emoji = "🟢" if conf >= 90 else "🟡" if conf >= 70 else "🔴"
        await update.message.reply_text(
            f"✅ *Email Found*\n\n"
            f"📧 `{r['email']}`\n"
            f"Confidence: {emoji} {conf}%\n"
            f"Method: {r['method']}",
            parse_mode='Markdown')

async def cmd_domain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/domain apple.com`", parse_mode='Markdown')
        return

    domain = ctx.args[0].lower().replace('www.','').replace('https://','').replace('http://','')
    await update.message.reply_text(f"🔍 Scanning *{domain}*...", parse_mode='Markdown')

    if not domain_has_email(domain):
        await update.message.reply_text(f"❌ *{domain}* has no mail server. Check the domain.")
        return

    pattern, emails = find_domain_pattern(domain)

    lines = [f"📧 *{domain}*\n"]
    lines.append(f"📐 Email pattern: `{pattern}`")

    if emails:
        lines.append(f"\n*Known emails found:*")
        for e in emails[:8]:
            lines.append(f"• `{e}`")
    else:
        lines.append("\nNo public emails found yet.")
        lines.append(f"\nGuess format: `firstname@{domain}` or `firstname.lastname@{domain}`")

    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

async def cmd_verify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/verify email@domain.com`", parse_mode='Markdown')
        return

    email = ctx.args[0].lower().strip()
    if '@' not in email:
        await update.message.reply_text("❌ Not a valid email format.")
        return

    await update.message.reply_text(f"🔍 Verifying `{email}`...", parse_mode='Markdown')

    domain = email.split('@')[1]

    # MX check
    mx = get_mx(domain)
    if not mx:
        await update.message.reply_text(
            f"❌ *{domain}* has no mail server.\nEmail cannot exist.",
            parse_mode='Markdown')
        return

    # Disify check
    d = disify_verify(email)
    dns_ok  = d['dns_ok'] if d else bool(mx)
    is_disp = d['disposable'] if d else False

    # SMTP check
    smtp = smtp_verify(email)

    if smtp is True:
        status, emoji = "VERIFIED ✅", "🟢"
        confidence = 98
    elif smtp is False:
        status, emoji = "INVALID ❌", "🔴"
        confidence = 5
    else:
        status, emoji = "LIKELY VALID 🟡", "🟡"
        confidence = 65

    await update.message.reply_text(
        f"📧 *Email Verification*\n\n"
        f"`{email}`\n\n"
        f"Status: {emoji} *{status}*\n"
        f"Confidence: {confidence}%\n"
        f"Mail server: ✅ {mx[:40]}\n"
        f"DNS: {'✅' if dns_ok else '❌'}\n"
        f"Disposable: {'⚠️ Yes' if is_disp else '✅ No'}",
        parse_mode='Markdown')

async def cmd_linkedin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/linkedin linkedin.com/in/johndoe`",
            parse_mode='Markdown')
        return

    url = ctx.args[0]
    if not url.startswith('http'):
        url = 'https://' + url

    await update.message.reply_text("🔍 Scanning LinkedIn profile...", parse_mode='Markdown')

    data = scrape_linkedin(url)

    if not data or not data.get('name'):
        await update.message.reply_text(
            "❌ Couldn't extract profile data.\n\n"
            "LinkedIn blocks scraping heavily.\n\n"
            "Instead: get their name + company, then use:\n"
            "`/find FirstName LastName company.com`",
            parse_mode='Markdown')
        return

    name = data.get('name','Unknown')
    company = data.get('company','Unknown')
    email = data.get('email')

    msg = f"👤 *{name}*\n🏢 {company}\n"

    if email:
        msg += f"\n📧 Email found: `{email}`"
    else:
        msg += f"\n❌ No email visible on profile\n\n"
        msg += f"Try: `/find {name} company.com`"

    await update.message.reply_text(msg, parse_mode='Markdown')

async def handle_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or '').strip()

    # Auto-detect LinkedIn URL
    if 'linkedin.com/in/' in text:
        ctx.args = [text]
        await cmd_linkedin(update, ctx)
        return

    # Auto-detect email address
    if re.match(r'^[\w.+\-]+@[\w\-]+\.[a-z]{2,}$', text):
        ctx.args = [text]
        await cmd_verify(update, ctx)
        return

    await update.message.reply_text(
        "Commands:\n"
        "`/find John Doe company.com`\n"
        "`/domain company.com`\n"
        "`/verify email@co.com`\n"
        "`/linkedin linkedin.com/in/user`\n\n"
        "Or paste a LinkedIn URL directly.",
        parse_mode='Markdown')

# ── Main ──────────────────────────────────────────────────────────────
def main():
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, fn in [
        ('start',    cmd_start),
        ('find',     cmd_find),
        ('domain',   cmd_domain),
        ('verify',   cmd_verify),
        ('linkedin', cmd_linkedin),
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    logger.info("📧 Email Finder Bot live")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
