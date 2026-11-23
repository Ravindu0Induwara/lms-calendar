#!/usr/bin/env python3
"""
LMS Course Material & Announcement Monitor + Calendar Sync
Monitors all enrolled Moodle courses for new PDFs, notes, slides, assignments, and announcements.
Sends instant push notifications via Telegram Bot.
"""

import os
import sys
import re
import json
import time
import requests

LMS_BASE_URL = os.environ.get("LMS_URL", "https://lms.tech.sjp.ac.lk").rstrip("/")
USERNAME = os.environ.get("LMS_USERNAME")
PASSWORD = os.environ.get("LMS_PASSWORD")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
STATE_FILE = os.environ.get("STATE_FILE", "seen_items.json")

TYPE_EMOJIS = {
    "resource": ("📄", "PDF / Lecture Note"),
    "assign": ("📝", "New Assignment"),
    "forum": ("📢", "Announcement / Forum"),
    "url": ("🔗", "Link / Resource"),
    "quiz": ("❓", "Quiz / Test"),
    "folder": ("📁", "Folder / Material Bundle"),
    "page": ("📃", "Page / Reading Note")
}


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Telegram credentials not set. Skipping notification.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            print("[+] Telegram message sent successfully.")
            return True
        else:
            print(f"[!] Telegram send error: {r.status_code} {r.text}")
            return False
    except Exception as e:
        print(f"[!] Failed to send Telegram: {e}")
        return False


class LMSMonitor:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        })

    def login(self, username, password) -> bool:
        login_url = f"{self.base_url}/login/index.php"
        print(f"[*] Logging into {self.base_url}...")
        try:
            self.session.get(login_url, timeout=25)
            r = self.session.post(login_url, data={
                "username": username,
                "password": password,
                "rememberusername": "1"
            }, timeout=25)
            if "login/index.php" in r.url and "Invalid login" in r.text:
                print("[!] Invalid LMS username or password.")
                return False
            print("[+] LMS login successful!")
            return True
        except Exception as e:
            print(f"[!] LMS login connection failed: {e}")
            return False

    def get_enrolled_courses(self):
        """Discovers all enrolled courses from dashboard and profile."""
        print("[*] Fetching enrolled courses...")
        courses = []
        seen_cids = set()

        for page in ["/my/", "/user/profile.php"]:
            try:
                resp = self.session.get(f"{self.base_url}{page}", timeout=25)
                matches = re.findall(
                    r'<a[^>]*href=["\'](?:https?://[^/]+)?/course/view\.php\?id=(\d+)["\'][^>]*>(.*?)</a>',
                    resp.text,
                    re.DOTALL
                )
                for cid, inner in matches:
                    clean_name = re.sub(r'<[^>]+>', '', inner).strip()
                    # Filter out non-course links or short noise
                    if clean_name and cid not in seen_cids and "view.php" not in clean_name and len(clean_name) > 3:
                        seen_cids.add(cid)
                        courses.append({
                            "id": cid,
                            "name": clean_name,
                            "url": f"{self.base_url}/course/view.php?id={cid}"
                        })
            except Exception as e:
                print(f"[!] Error reading {page}: {e}")

        print(f"[+] Found {len(courses)} enrolled courses.")
        return courses

    def scan_course_modules(self, course: dict):
        """Scans a course page for modules (PDFs, notes, assignments, announcements)."""
        cid = course["id"]
        cname = course["name"]
        url = course["url"]
        items = []

        try:
            resp = self.session.get(url, timeout=25)
            html = resp.text

            # Parse activity items: <li class="activity ... id="module-XXXX">
            modules = re.findall(
                r'<li[^>]*class=["\']activity\s+([^"\']+)["\'][^>]*id=["\']module-(\d+)["\']>(.*?)</li>',
                html,
                re.DOTALL
            )

            for modtype_classes, mid, inner in modules:
                # Name
                name_match = re.search(r'<span class=["\']instancename["\']>(.*?)<span class=["\']accesshide["\']', inner, re.DOTALL)
                if not name_match:
                    name_match = re.search(r'<span class=["\']instancename["\']>(.*?)</span>', inner, re.DOTALL)
                title = re.sub(r'<[^>]+>', '', name_match.group(1)).strip() if name_match else "Resource"

                # Link
                link_match = re.search(r'href=["\'](https?://[^/]+/mod/([^/]+)/view\.php\?id=\d+)["\']', inner)
                link = link_match.group(1) if link_match else f"{self.base_url}/mod/resource/view.php?id={mid}"
                modname = link_match.group(2) if link_match else modtype_classes.split()[0]

                items.append({
                    "id": f"mod-{mid}",
                    "mid": mid,
                    "type": modname,
                    "title": title,
                    "url": link,
                    "course_id": cid,
                    "course_name": cname
                })

                # If this is an announcements forum, also check for discussion topics
                if modname == "forum" and ("announcement" in title.lower() or "news" in title.lower()):
                    try:
                        f_resp = self.session.get(link, timeout=15)
                        discussions = re.findall(
                            r'<a[^>]*href=["\'](https?://[^/]+/mod/forum/discuss\.php\?d=(\d+))["\'][^>]*>(.*?)</a>',
                            f_resp.text
                        )
                        for d_url, did, d_title in discussions:
                            d_clean = re.sub(r'<[^>]+>', '', d_title).strip()
                            if d_clean:
                                items.append({
                                    "id": f"disc-{did}",
                                    "mid": did,
                                    "type": "announcement",
                                    "title": d_clean,
                                    "url": d_url,
                                    "course_id": cid,
                                    "course_name": cname
                                })
                    except Exception:
                        pass

        except Exception as e:
            print(f"[!] Error scanning course {cid}: {e}")

        return items


def load_state() -> set:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("seen_ids", []))
        except Exception:
            pass
    return set()


def save_state(seen_ids: set):
    os.makedirs(os.path.dirname(os.path.abspath(STATE_FILE)), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen_ids": sorted(list(seen_ids)), "last_updated": int(time.time())}, f, indent=2)


def main():
    if not USERNAME or not PASSWORD:
        print("[!] Missing LMS_USERNAME or LMS_PASSWORD.")
        sys.exit(1)

    monitor = LMSMonitor(LMS_BASE_URL)
    if not monitor.login(USERNAME, PASSWORD):
        sys.exit(1)

    seen_ids = load_state()
    is_initial_run = len(seen_ids) == 0

    courses = monitor.get_enrolled_courses()
    all_current_items = []

    print(f"[*] Scanning {len(courses)} courses for course materials & announcements...")
    for c in courses:
        items = monitor.scan_course_modules(c)
        all_current_items.extend(items)
        time.sleep(0.3)

    print(f"[+] Total active materials indexed: {len(all_current_items)}")

    if is_initial_run:
        print("[*] Initial setup run: registering all existing materials into state...")
        for item in all_current_items:
            seen_ids.add(item["id"])
        save_state(seen_ids)

        # Send Telegram confirmation
        welcome_msg = (
            "<b>🔔 LMS Notification Bot Activated!</b>\n\n"
            f"✅ Successfully linked to: <b>{LMS_BASE_URL}</b>\n"
            f"📚 Enrolled Courses Tracked: <b>{len(courses)}</b>\n"
            f"📄 Current Materials Indexed: <b>{len(all_current_items)}</b>\n\n"
            "<i>You will receive an instant alert here whenever a lecturer uploads any new PDF, lecture note, announcement, or assignment!</i>"
        )
        send_telegram(welcome_msg)
        print("[+] Initial state initialized and Telegram welcome message dispatched.")
        return

    # Check for new items
    new_items = [it for it in all_current_items if it["id"] not in seen_ids]

    if not new_items:
        print("[*] No new materials or announcements detected.")
    else:
        print(f"[🎉] Detected {len(new_items)} NEW item(s)!")
        for it in new_items:
            emoji, type_label = TYPE_EMOJIS.get(it["type"], ("📌", "New Material"))
            msg = (
                f"<b>{emoji} New {type_label} Uploaded!</b>\n\n"
                f"📚 <b>Course:</b> {it['course_name']}\n"
                f"📌 <b>Title:</b> {it['title']}\n\n"
                f"🔗 <a href='{it['url']}'>Open on LMS</a>"
            )
            print(f"    - Alerting: {it['course_name']} -> {it['title']}")
            send_telegram(msg)
            seen_ids.add(it["id"])
            time.sleep(1)

        save_state(seen_ids)
        print("[+] Updated state file saved.")


if __name__ == "__main__":
    main()
