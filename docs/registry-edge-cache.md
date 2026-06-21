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
3. **Re-push** your release(s) so blobs populate R2 (one-time uplink cost):
   `balena push <fleet>`.
4. **Verify** a device pulls from the edge — its engine should follow a `307` to
   `*.r2.cloudflarestorage.com` (or your custom domain), not your host:
   ```bash
   curl -k -sI -H "Authorization: Bearer <reg-token>" \
     https://registry2.<PUBLIC_TLD>/v2/<repo>/blobs/<digest> | grep -i location
   ```

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
