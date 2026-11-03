# Bisque grid check

Finds product images that don't fit the collection grid, lets you review them, and converts and replaces the ones you approve in Shopify.

## What it does

1. **Scan catalog**: checks the first image, the second image or both, for active products only, active and drafts, or everything including archived. Read-only. **Stop** halts any running job after the current item.
2. **Fix specific products**: paste product links, handles or IDs. Off-grid images are selected for you.
3. **Review**: every image is shown in a card with the same proportions and colour as the shop grid, with the fit box drawn on it.
4. **Convert**: cuts out the product (if needed), scales it until it touches the fit box and centres it on the canvas. Nothing changes in Shopify; you see before and after.
5. **Replace in Shopify**: backs up the original, uploads the new file and swaps it in place. Gallery position, alt text and variant links stay intact. The result is re-checked on the live image.
6. **Restore original**: puts the backed-up original back.

Product types: the scan panel lists all product types from Shopify. Unticked types are skipped in scans and hidden from the list; **Skip apparel** unticks clothing and footwear in one click (check the result, it matches on the type name). Also skipped automatically: detail shots that fill the frame, and model or lifestyle shots. A model shot is recognised by a busy background, a face, or a body cut off by the top or bottom edge. Model shots never appear in the flatlay views; they have their own **Model shots** filter. If the app gets one wrong, use **Model shot** or **Not a model shot** under the image; that choice is kept on every later scan.

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

**Size by height** (default): every product gets the same height (the fit box height) and the width may vary up to the max width. This keeps grid rows level. **Size by fit box**: each product is scaled until it touches the box, so wide items end up shorter.

Canvas and height follow the Notion page "Richtlijnen productfotografie": canvas 2000 × 2000, product height 1256 px. Wide products keep a side margin in the grid: 10% of the card width on each side by default (setting "Side margin in the grid"), which makes the max width 1185 px on a 2000 px canvas.

The shop theme scales each image to the card's height and crops the sides, so a square canvas in the 374 × 505 card only shows its middle 1481 px. The app never makes a product wider than that visible part minus the side margin, flags existing images that are cut off at the sides, and the review screen crops images the same way the shop does. If the card shape changes, update the card width and height under Size standard. Change them under **Size standard** in the app, then scan again. All checks and conversions use the saved values.

## Notes

- **Reset list** clears all scan results and converted images. Images already replaced in Shopify stay replaced, and backups of originals stay available under Download originals.

- One job runs at a time (scan, convert, replace or restore). Progress shows at the top.
- Originals can be downloaded as one zip under Size standard, "Download originals".
- A backup is only made once per image, so the first original is never overwritten by a later replacement.
