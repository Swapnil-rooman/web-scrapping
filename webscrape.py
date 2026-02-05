import asyncio
import json
import re
import time
import os
import boto3
import hashlib
from decimal import Decimal
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright

MAX_LINKS_PER_SITE = 15
CONCURRENT_ARTICLES = 5
OUTPUT_FILE = "/tmp/scraped_data.json"  # Lambda writable path

# -----------------------------
# URL HEURISTIC FILTER
# -----------------------------

def looks_like_article(url):
    url = url.lower()

    # Blacklist: exclude these terms
    bad = [
        "login", "signup", "subscribe", "register", "forgot-password",
        "category", "tag", "author", "archive", "page=", "#",
        "javascript:", "mailto:", "contact", "about-us", "privacy",
        "terms-of-use", "terms-and-conditions", "cookie", "sitemap",
        "disclaimer", "help", "faq", "feedback", "advertisement",
        "ads", "sponsored", "careers", "jobs", "partner", "advertise",
        "media-kit", "benefits", "pricing", "plan", "subscription",
        "account", "profile", "settings", "dashboard", "newsletter",
        "download", ".pdf", ".zip", ".doc", ".mp4", ".jpg", ".png",
        "rss", "feed", "xml", "json", ".css", ".js", "api/", "admin",
        "search?", "q=", "s=", "gallery", "video", "image", "photo"
    ]

    if any(b in url for b in bad):
        return False

    # Whitelist: must contain one of these keywords
    good = [
        "article", "news", "press", "release", "post", "blog", "story",
        "breaking", "report", "analysis", "update", "alert", "headline",
        "coverage", "dispatch", "bulletin", "feature", "interview"
    ]

    if not any(g in url for g in good):
        return False

    path = urlparse(url).path

    # Must have at least 2 path segments (e.g., /2026/02/article-title/)
    if path.count("/") < 2:
        return False

    # Must look like a news URL with date or slug
    if "-" in path or re.search(r"\d{4}", path) or re.search(r"\d{2}/", path):
        return True

    return False


# -----------------------------
# STRUCTURED DATA EXTRACTION
# -----------------------------

async def extract_json_ld(page):
    try:
        scripts = await page.query_selector_all("script[type='application/ld+json']")
        for s in scripts:
            txt = await s.text_content()
            if not txt:
                continue

            data = json.loads(txt)

            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") in ["NewsArticle", "Article", "BlogPosting"]:
                        return item

            if isinstance(data, dict):
                if data.get("@type") in ["NewsArticle", "Article", "BlogPosting"]:
                    return data
    except:
        pass

    return None


async def get_meta(page, name):
    el = await page.query_selector(f"meta[property='{name}'], meta[name='{name}']")
    if el:
        return await el.get_attribute("content")
    return None


# -----------------------------
# SELECTOR FALLBACK
# -----------------------------

async def extract_first(page, selectors):
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                txt = (await el.text_content() or "").strip()
                if len(txt) > 5:
                    return txt
        except:
            pass
    return None


HEADINGS = ["h1", "[class*='title']", "[class*='headline']"]
SUBS = ["h2", "[class*='subtitle']", "[class*='excerpt']", "article p"]
DATES = ["time", "[class*='date']", "[class*='publish']"]


# -----------------------------
# ARTICLE SCRAPER
# -----------------------------

async def scrape_article(context, url):
    page = await context.new_page()

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except:
            pass

        data = {
            "url": url,
            "heading": None,
            "subheading": None,
            "date": None
        }

        # ---------- JSON LD ----------
        ld = await extract_json_ld(page)
        if ld:
            data["heading"] = ld.get("headline")
            data["subheading"] = ld.get("description")
            data["date"] = ld.get("datePublished")

        # ---------- META ----------
        if not data["heading"]:
            data["heading"] = await get_meta(page, "og:title")

        if not data["subheading"]:
            data["subheading"] = await get_meta(page, "og:description")

        if not data["date"]:
            data["date"] = await get_meta(page, "article:published_time")

        # ---------- SELECTOR ----------
        if not data["heading"]:
            data["heading"] = await extract_first(page, HEADINGS)

        if not data["subheading"]:
            data["subheading"] = await extract_first(page, SUBS)

        if not data["date"]:
            data["date"] = await extract_first(page, DATES)

        # clean
        for k in data:
            if isinstance(data[k], str):
                data[k] = re.sub(r"\s+", " ", data[k]).strip()

        print("✓", (data["heading"] or "No heading")[:80])

        await page.close()
        return data

    except Exception as e:
        await page.close()
        print("✗ failed:", url)
        return None


# -----------------------------
# LINK DISCOVERY
# -----------------------------

async def get_article_links(page, base_url):
    print("\nScanning", base_url)

    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=60000)

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except:
            pass

        # Target article-like selectors only
        article_selectors = [
            "article a",
            "a[href*='article']",
            "a[href*='news']",
            "a[href*='press']",
            "a[href*='post']",
            ".article-link",
            ".news-link",
            ".post-link",
            ".story-link",
            "[class*='article'] a",
            "[class*='news'] a",
            "[class*='post'] a",
        ]

        all_links = set()

        for selector in article_selectors:
            try:
                elements = await page.query_selector_all(selector)
                for el in elements[:50]:  # limit per selector
                    href = await el.get_attribute("href")
                    if href:
                        all_links.add(href)
            except:
                pass

        # Fallback: get all links if specific selectors failed
        if not all_links:
            try:
                links = await page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => e.href)"
                )
                all_links.update(links)
            except:
                pass

        print(f"Raw links found: {len(all_links)}")

        clean = set()
        base_domain = urlparse(base_url).netloc

        for link in all_links:
            if not link:
                continue

            full = urljoin(base_url, link)
            parsed = urlparse(full)

            # Same domain check
            if base_domain not in parsed.netloc:
                continue

            # Apply article filter
            if looks_like_article(full):
                clean.add(full)

        print(f"✓ Valid article links: {len(clean)}")

        # debug sample
        for s in list(clean)[:3]:
            print(f"  → {s}")

        return list(clean)[:MAX_LINKS_PER_SITE]

    except Exception as e:
        print(f"⚠ Link discovery error: {e}")
        return []


# -----------------------------
# MAIN DRIVER
# -----------------------------

async def main():

    start_time = time.time()
    print("=" * 60)
    print("SCRAPING STARTED")
    print("=" * 60)

    sites = [
       "https://impact.indiaai.gov.in/media-resources?tab=press",
        "https://indiaai.gov.in/articles/all",
        "https://negd.gov.in/press-release/",
        "https://cio.economictimes.indiatimes.com/news/artificial-intelligence?utm_source=main_menu2&utm_medium=homepage",
        "https://www.newsonair.gov.in/category/national/",
        "https://cmogujarat.gov.in/en/news",
        "https://timesofindia.indiatimes.com/technology/artificial-intelligence",
        "https://www.hindustantimes.com/technology",
        "https://ai.economictimes.com/",
        "https://www.rswebsols.com/category/technology/",
        "https://globalvoices.org/-/topics/technology/",
        "http://analyticsindiamag.com/ai-news",
        "https://tele.net.in/category/artificial-intelligence/",
        "https://hubnetwork.in/?s=artificial+intelligence",
        "https://rajbhavan.mizoram.gov.in/?s=artificial+intelligence",
        "https://www.newindianexpress.com/search?q=artificial%20intelligence",
        "https://www.visive.ai/_/search?query=Artificial%20Intelligence",
        "https://nbbgc.org/?s=artificial+intelligence",
        "https://www.thehindu.com/sci-tech/technology/",
        "https://www.communicationstoday.co.in/?s=artificial+intelligence",
        "https://www.eletimes.ai/?s=artificial+intelligence",
        "https://www.databreachtoday.com/latest-news",
        "https://indianexpress.com/section/technology/artificial-intelligence/?ref=technology_pg",
        "https://www.news18.com/tech/",
        "https://theprint.in/?s=artificial+intelligence",
        "https://www.aninews.in/search/?query=artificial+intelligence",
        "https://egov.eletsonline.com/?s=artificial%20intelligence"
    ]

    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--single-process"]
        )

        for site in sites:
            site_start = time.time()
            context = await browser.new_context()
            page = await context.new_page()

            try:
                links = await get_article_links(page, site)

                sem = asyncio.Semaphore(CONCURRENT_ARTICLES)

                async def worker(link):
                    async with sem:
                        return await scrape_article(context, link)

                tasks = [worker(l) for l in links]
                site_results = await asyncio.gather(*tasks)

                results.extend([r for r in site_results if r])

            except Exception as e:
                print("Site failed:", site)

            await context.close()
            
            site_elapsed = time.time() - site_start
            print(f"✓ Site completed in {site_elapsed:.2f}s\n")

        await browser.close()

    # ---------------- SAVE JSON ----------------

    if results:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        print("\nSaved", len(results), f"articles to {OUTPUT_FILE}")

    else:
        print("\nNo articles scraped")
    
    # ============ TIMER SUMMARY ============
    total_elapsed = time.time() - start_time
    minutes = int(total_elapsed // 60)
    seconds = total_elapsed % 60
    
    print("\n" + "=" * 60)
    print(f"SCRAPING COMPLETED in {minutes}m {seconds:.2f}s")
    print(f"Total Time: {total_elapsed:.2f} seconds")
    print("=" * 60)


def upload_to_s3(file_path, bucket_name, object_name=None):
    if object_name is None:
        object_name = os.path.basename(file_path)

    s3_client = boto3.client('s3')
    try:
        response = s3_client.upload_file(file_path, bucket_name, object_name)
        print(f"Successfully uploaded {file_path} to s3://{bucket_name}/{object_name}")
    except Exception as e:
        print(f"Error uploading to S3: {e}")
        return False
    return True


def save_to_dynamodb(articles, table_name):
    """Save articles to DynamoDB table"""
    if not articles or not table_name:
        print("No articles or table name provided")
        return False
    
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)
    
    saved_count = 0
    failed_count = 0
    
    # Current timestamp for all articles in this batch
    scraped_timestamp = int(time.time())
    # TTL: 90 days from now (matching S3 lifecycle)
    ttl = scraped_timestamp + (90 * 24 * 60 * 60)
    
    for article in articles:
        try:
            # Generate unique article_id from URL
            article_id = hashlib.md5(article['url'].encode()).hexdigest()
            
            # Prepare item for DynamoDB
            item = {
                'article_id': article_id,
                'scraped_at': scraped_timestamp,
                'url': article['url'],
                'heading': article.get('heading') or 'N/A',
                'subheading': article.get('subheading') or 'N/A',
                'date': article.get('date') or 'N/A',
                'ttl': ttl
            }
            
            # Put item into DynamoDB
            table.put_item(Item=item)
            saved_count += 1
            
        except Exception as e:
            print(f"Error saving article {article.get('url')}: {e}")
            failed_count += 1
    
    print(f"\n✓ DynamoDB: Saved {saved_count} articles, {failed_count} failed")
    return saved_count > 0


# -----------------------------
# LAMBDA HANDLER
# -----------------------------

def handler(event, context):
    print("Lambda handler started")
    asyncio.run(main())
    
    # Upload to S3
    bucket_name = os.environ.get("S3_BUCKET_NAME")
    if bucket_name:
        if os.path.exists(OUTPUT_FILE):
             # Append timestamp to filename to prevent overwriting
            timestamp = int(time.time())
            s3_key = f"scraped_data_{timestamp}.json"
            upload_to_s3(OUTPUT_FILE, bucket_name, s3_key)
        else:
            print("No output file generated to upload.")
    else:
        print("S3_BUCKET_NAME environment variable not set. Skipping upload.")
    
    # Save to DynamoDB
    table_name = os.environ.get("DYNAMODB_TABLE_NAME")
    if table_name:
        if os.path.exists(OUTPUT_FILE):
            try:
                with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                    articles = json.load(f)
                save_to_dynamodb(articles, table_name)
            except Exception as e:
                print(f"Error reading JSON for DynamoDB: {e}")
        else:
            print("No output file to save to DynamoDB.")
    else:
        print("DYNAMODB_TABLE_NAME environment variable not set. Skipping DynamoDB save.")
        
    return {
        "statusCode": 200,
        "body": json.dumps("Scraping completed!")
    }

# -----------------------------
# LOCAL RUN
# -----------------------------

if __name__ == "__main__":
    # For local testing, we can just run main
    asyncio.run(main())

