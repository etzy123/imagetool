# Bisque grid check

Finds product images that don't fit the collection grid, lets you review them, and converts and replaces the ones you approve in Shopify.

## What it does

1. **Scan catalog**: checks the first and second image of every matching product against the size standard. Read-only.
2. **Fix specific products**: paste product links, handles or IDs. Off-grid images are selected for you.
3. **Review**: every image is shown in a card with the same proportions and colour as the shop grid, with the fit box drawn on it.
4. **Convert**: cuts out the product (if needed), scales it until it touches the fit box and centres it on the canvas. Nothing changes in Shopify; you see before and after.
5. **Replace in Shopify**: backs up the original, uploads the new file and swaps it in place. Gallery position, alt text and variant links stay intact. The result is re-checked on the live image.
6. **Restore original**: puts the backed-up original back.

Skipped automatically: product types in the skip list (default `Footwear`), model and lifestyle shots, and detail shots that fill the frame.

## Deploy on Railway

1. Push this folder to a GitHub repo and create a Railway service from it. Railway builds the Dockerfile.
2. Add a **volume** mounted at `/data` (scan results and backups of originals live here).
3. Set the variables below and deploy. Use at least 2 GB of memory; the cutout model needs it.

| Variable | Value |
|---|---|
| `APP_PASSWORD` | Password for the app (any username works in the login prompt) |
| `SHOPIFY_STORE` | `bisquegolfdev.myshopify.com` |
| `SHOPIFY_ACCESS_TOKEN` | Admin API token (`shpat_...`), **or** use the two below |
| `SHOPIFY_CLIENT_ID` / `SHOPIFY_CLIENT_SECRET` | For an app made in the Shopify Dev Dashboard; the app fetches its own token |

Required Admin API scopes: `read_products`, `write_products`, `read_files`, `write_files`.

## Size standard

Defaults follow the Notion page "Richtlijnen productfotografie": canvas 2000 × 2000, fit box 1256 × 1256. Change them under **Size standard** in the app, then scan again. All checks and conversions use the saved values.

## Notes

- One job runs at a time (scan, convert, replace or restore). Progress shows at the top.
- Originals can be downloaded as one zip under Size standard, "Download originals".
- A backup is only made once per image, so the first original is never overwritten by a later replacement.
