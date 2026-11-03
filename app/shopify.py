"""Shopify Admin GraphQL client.

Images are replaced in place with fileUpdate, which keeps each image's gallery
position, alt text and variant links. New files go through Shopify's own
staged-upload storage, so no external image hosting is needed.
"""
import os
import re
import time

import requests

API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10")

PRODUCT_FIELDS = """
  id title handle productType vendor status
  media(first: 8) {
    nodes {
      id mediaContentType
      ... on MediaImage { image { url width height } }
    }
  }
"""


class ShopifyError(Exception):
    pass


class Shopify:
    def __init__(self):
        self.shop = os.environ["SHOPIFY_STORE"].replace("https://", "").strip("/")
        self.static_token = os.getenv("SHOPIFY_ACCESS_TOKEN")
        self.client_id = os.getenv("SHOPIFY_CLIENT_ID")
        self.client_secret = os.getenv("SHOPIFY_CLIENT_SECRET")
        self._token, self._expires = None, 0
        if not self.static_token and not (self.client_id and self.client_secret):
            raise ShopifyError("Set SHOPIFY_ACCESS_TOKEN, or SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET")

    def token(self):
        if self.static_token:
            return self.static_token
        if not self._token or time.time() > self._expires - 300:
            r = requests.post(f"https://{self.shop}/admin/oauth/access_token", timeout=30, json={
                "client_id": self.client_id, "client_secret": self.client_secret,
                "grant_type": "client_credentials"})
            if r.status_code != 200:
                raise ShopifyError(f"Could not get Shopify token ({r.status_code}): {r.text[:200]}")
            d = r.json()
            self._token, self._expires = d["access_token"], time.time() + int(d.get("expires_in", 86400))
        return self._token

    def gql(self, query, variables=None):
        url = f"https://{self.shop}/admin/api/{API_VERSION}/graphql.json"
        for attempt in range(8):
            r = requests.post(url, timeout=60, json={"query": query, "variables": variables or {}},
                              headers={"X-Shopify-Access-Token": self.token()})
            if r.status_code in (429, 502, 503):
                time.sleep(2 + attempt * 2)
                continue
            if r.status_code != 200:
                raise ShopifyError(f"Shopify HTTP {r.status_code}: {r.text[:300]}")
            d = r.json()
            errs = d.get("errors")
            if errs:
                if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errs):
                    time.sleep(2 + attempt * 2)
                    continue
                raise ShopifyError("; ".join(e.get("message", str(e)) for e in errs))
            return d["data"]
        raise ShopifyError("Shopify kept throttling the request")

    # ---- reading products -------------------------------------------------

    def collection_id(self, handle):
        d = self.gql("query($q:String!){collections(first:1, query:$q){nodes{id}}}", {"q": f"handle:{handle}"})
        nodes = d["collections"]["nodes"]
        if not nodes:
            raise ShopifyError(f"Collection '{handle}' not found")
        return nodes[0]["id"].rsplit("/", 1)[1]

    def product_types(self):
        """All product types in the store."""
        try:
            d = self.gql("query{productTypes(first:250){nodes}}")
            types = d["productTypes"]["nodes"]
        except ShopifyError:
            d = self.gql("query{shop{productTypes(first:250){edges{node}}}}")
            types = [e["node"] for e in d["shop"]["productTypes"]["edges"]]
        return sorted({t for t in types if t}, key=str.lower)

    def count_products(self, q):
        d = self.gql("query($q:String){productsCount(query:$q, limit:null){count}}", {"q": q})
        return d["productsCount"]["count"]

    def iter_products(self, q):
        after = None
        while True:
            d = self.gql(
                "query($q:String,$after:String){products(first:50, after:$after, query:$q){"
                f"nodes{{{PRODUCT_FIELDS}}} pageInfo{{hasNextPage endCursor}}}}}}",
                {"q": q, "after": after})
            p = d["products"]
            yield from p["nodes"]
            if not p["pageInfo"]["hasNextPage"]:
                break
            after = p["pageInfo"]["endCursor"]

    def product(self, ref):
        kind, val = parse_product_ref(ref)
        if kind == "id":
            d = self.gql(f"query($id:ID!){{product(id:$id){{{PRODUCT_FIELDS}}}}}",
                         {"id": f"gid://shopify/Product/{val}"})
            return d["product"]
        d = self.gql(f"query($q:String){{products(first:1, query:$q){{nodes{{{PRODUCT_FIELDS}}}}}}}",
                     {"q": f"handle:'{val}'"})
        nodes = d["products"]["nodes"]
        return nodes[0] if nodes else None

    # ---- writing images ---------------------------------------------------

    def staged_upload(self, data: bytes, filename: str, mime: str) -> str:
        d = self.gql("""mutation($input:[StagedUploadInput!]!){stagedUploadsCreate(input:$input){
            stagedTargets{url resourceUrl parameters{name value}} userErrors{field message}}}""",
                     {"input": [{"resource": "IMAGE", "filename": filename, "mimeType": mime, "httpMethod": "POST"}]})
        res = d["stagedUploadsCreate"]
        if res["userErrors"]:
            raise ShopifyError(str(res["userErrors"]))
        t = res["stagedTargets"][0]
        form = {p["name"]: p["value"] for p in t["parameters"]}
        for attempt in range(4):
            r = requests.post(t["url"], data=form, files={"file": (filename, data, mime)}, timeout=120)
            if r.status_code in (200, 201, 204):
                return t["resourceUrl"]
            time.sleep(3)
        raise ShopifyError(f"Staged upload failed ({r.status_code}): {r.text[:200]}")

    def replace_image(self, media_gid: str, resource_url: str, timeout=120):
        """Swap the file behind an existing product image, then wait until it is ready."""
        d = self.gql("""mutation($files:[FileUpdateInput!]!){fileUpdate(files:$files){
            files{id fileStatus} userErrors{field message code}}}""",
                     {"files": [{"id": media_gid, "originalSource": resource_url}]})
        res = d["fileUpdate"]
        if res["userErrors"]:
            raise ShopifyError("; ".join(e["message"] for e in res["userErrors"]))
        start = time.time()
        while time.time() - start < timeout:
            time.sleep(4)
            n = self.gql("""query($id:ID!){node(id:$id){... on MediaImage{
                fileStatus fileErrors{message} image{url width height}}}}""", {"id": media_gid})["node"]
            if n["fileStatus"] == "READY" and n.get("image"):
                return n["image"]
            if n["fileStatus"] == "FAILED":
                raise ShopifyError("Shopify could not process the image: " +
                                   "; ".join(e["message"] for e in n.get("fileErrors") or []))
        raise ShopifyError("Shopify did not finish processing the image in time")


def parse_product_ref(ref: str):
    """Accepts a shop URL, admin URL, product GID, numeric ID or handle."""
    ref = ref.strip()
    m = re.search(r"gid://shopify/Product/(\d+)", ref)
    if m:
        return "id", m.group(1)
    if ref.isdigit():
        return "id", ref
    m = re.search(r"/products/([^/?#\s]+)", ref)
    if m:
        v = m.group(1)
        return ("id", v) if v.isdigit() else ("handle", v)
    return "handle", ref.strip("/")


def sized_url(url: str, width: int) -> str:
    return url + ("&" if "?" in url else "?") + f"width={width}"


def download(url: str) -> bytes:
    for attempt in range(4):
        try:
            r = requests.get(url, timeout=90)
            if r.status_code == 200:
                return r.content
        except requests.RequestException:
            pass
        time.sleep(2 + attempt * 2)
    raise ShopifyError(f"Could not download {url}")
