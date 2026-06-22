# Registry edge cache (cloud object storage)

The problem this solves: openBalena's registry stores image blobs in S3 (by default, a `minio`
container on the host). Every device in the field pulls **full image blobs through your host's
uplink**. For large images (multi-GB ROS/ML containers) on a low-upload-bandwidth host, that's the
single worst bottleneck — N devices × GBs, all funneled through one slow uplink.

## The fix
Point the registry's S3 backend at **cloud object storage** and enable the registry's **blob
redirect**. Then:

```
builder → registry → uploads each blob ONCE to cloud storage   (uplink hit once, at build time)
device  → registry: GET blob X  → 307 redirect to the cloud URL
device  → downloads blob directly from the cloud edge          (never touches your host)
```

Your uplink is hit **once per image at build time**, not once per device-pull. Devices stream from
the cloud's bandwidth/CDN. Deltas (open-balena-delta) still layer on top for incremental updates.

## Recommended backend: Cloudflare R2
| | Cloudflare **R2** | DigitalOcean **Spaces** | AWS **S3** |
|---|---|---|---|
| Egress (device pulls) | **free** | metered | metered (expensive) |
| S3-compatible | yes (`region=auto`) | yes | yes |
| Edge/CDN | Cloudflare global edge | DO CDN | CloudFront extra |

**R2 wins for fleet-scale image serving** (zero egress — pulls don't cost per-GB) and fits naturally
if you already run Cloudflare DNS (see [public-ingress.md](public-ingress.md)).

## Setup (R2)
1. **Create a bucket** (e.g. `openbalena-registry`) and an **R2 API token** (Object Read & Write).
   You get an **Access Key ID**, **Secret Access Key**, and the S3 endpoint
   `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`.
2. **Repoint the registry storage** at R2 and enable redirect. Override these on the `registry`
   service (compose override or your secrets store):
   ```yaml
   services:
     registry:
       environment:
         REGISTRY2_S3_REGION_ENDPOINT: "<ACCOUNT_ID>.r2.cloudflarestorage.com"
         REGISTRY2_S3_BUCKET: "openbalena-registry"
         REGISTRY2_S3_REGION: "auto"
         REGISTRY2_S3_KEY: "${R2_ACCESS_KEY_ID}"
         REGISTRY2_S3_SECRET: "${R2_SECRET_ACCESS_KEY}"
         REGISTRY2_S3_FORCEPATHSTYLE: "true"
         # 307 blob downloads to R2 instead of streaming through the registry:
         REGISTRY_STORAGE_REDIRECT_DISABLE: "false"
   ```
   (Exact env names follow your registry image's confd template; the docker distribution registry
   also honours `REGISTRY_STORAGE_S3_*` overrides directly.)
3. **Populate R2.** Either **re-push** every release (`balena push <fleet>` — one-time uplink cost),
   **or migrate the existing blobs** from your current store (minio) to R2 (see below). Do this
   **before** repointing the registry, or do it and then verify completeness (next step) — a device
   assigned to a release whose blobs aren't all in R2 will fail to pull.
4. **Verify the migration is COMPLETE — this is mandatory, not optional.**
   A partial blob copy is the nastiest failure mode here: the big layer blobs copy fine, but if even
   one small **manifest/config JSON blob** is missing, the registry resolves the manifest *link* →
   tries to read the manifest *blob* → 404 `manifest unknown`, and **every** device pull dies at 0%
   with `manifest for …@sha256:… not found`. The device looks broken; it is not — the registry's
   store is. **Reflashing the device will NOT help** (the fault is server-side). Check blob-count
   parity and that a previously-failing manifest now serves:
   ```bash
   # object-count parity, source store vs R2 (must be EQUAL)
   mc ls --recursive src/<bucket>/data/docker/registry/v2/blobs/ | wc -l
   mc ls --recursive r2/<bucket>/data/docker/registry/v2/blobs/  | wc -l
   # the registry must serve a real manifest (200) and 307 a blob to R2:
   curl -k -sI -H "Authorization: Bearer <reg-token>" \
     https://registry2.<PUBLIC_TLD>/v2/<repo>/manifests/<digest>      # -> 200, not 404
   curl -k -sI -H "Authorization: Bearer <reg-token>" \
     https://registry2.<PUBLIC_TLD>/v2/<repo>/blobs/<digest> | grep -i location   # -> r2 URL
   ```

### Migrating existing blobs (minio → R2) without losing the small ones
Don't hand-copy only `blobs/` or only the big objects — you **must** copy the entire
`data/docker/registry/v2/` tree (both `blobs/` **and** `repositories/`, including every tiny
`link` and every manifest/config blob). Use `mc mirror` between two S3 endpoints so nothing is
selectively dropped:
```bash
# source = your minio (fronted on :80 inside the s3 container); creds = REGISTRY2_S3_KEY/SECRET
mc alias set src http://<s3-host>:80 "$REGISTRY2_S3_KEY" "$REGISTRY2_S3_SECRET"
mc alias set r2  https://<ACCOUNT_ID>.r2.cloudflarestorage.com "$R2_KEY" "$R2_SECRET"
mc mirror --overwrite src/<bucket>/data r2/<bucket>/data      # idempotent; re-run until counts match
```
`mc mirror` only transfers missing/changed objects, so it's safe to re-run; re-run it until the
blob counts in step 4 are exactly equal. (A plain filesystem copy of the minio volume does **not**
work — modern minio stores objects as `xl.meta`/`part.N`, not 1:1 files.)

## Optional: custom domain
Map an R2 custom domain via your Cloudflare DNS (e.g. `registry-cache.<your-domain>`) so redirect
URLs are on your domain and cacheable at Cloudflare's edge.

## Notes / gotchas
- **Private bucket + presigned URLs:** with a private bucket the registry returns time-limited
  presigned URLs; devices must reach the R2 endpoint (public internet — fine for field devices).
- **The builder still uploads once.** If your build host *also* has a bad uplink, the one-time
  build-push to R2 is still slow — but it's once, not per device.
- **Internal TLS:** if you keep a local S3/minio for anything, the registry→local-S3 hop is
  self-signed; see the `REGISTRY_STORAGE_S3_SKIPVERIFY` note in [../components/builder/README.md](../components/builder/README.md).
