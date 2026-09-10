# Compliance and politeness

This project is a **personal, low-rate transparency tracker** of publicly
floated government tenders and published award results in Maharashtra. It reads
only pages that are already public.

## Rules the crawlers follow
- **Rate**: at most 1 request per 2 seconds per portal by default; exponential
  backoff on HTTP 429/5xx; back off for an hour and log if a portal blocks us.
- **No captcha defeating**: captchas are never solved with third-party services.
  Where a page is captcha-gated (e.g. the NIC GePNIC Results/AOC section), a
  human solves the portal's own captcha and the crawl runs only inside that
  authorised session. Anything still gated goes to a manual queue.
- **Identification**: a realistic, honest User-Agent; no attempt to impersonate
  or evade beyond that.
- **Read-only**: no form submissions other than the searches a normal user
  would run; no writes, no account creation.
- **Documents**: PDFs are fetched selectively (above a value threshold in
  backfill) to limit load and storage.

## Terms of use
NIC's GePNIC portals (mahatenders.gov.in and eprocure.gov.in) and other state
portals publish terms on access. Automated access should be low-rate and
non-disruptive. This tracker is operated for personal monitoring and public
transparency, not resale, and honors the rate and captcha rules above. If any
portal operator requests it, crawling of that portal will stop.
