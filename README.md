# DNS Updater

Small Kubernetes CronJob for maintaining Google Cloud DNS `A` records from the current public IPv4 address.

The intent is to replace the Node-RED dynamic DNS flow with something boring, config-driven, and easy to inspect:

1. Resolve the current public IP from one or more HTTPS sources.
2. Read each configured Google Cloud DNS record.
3. Apply a Cloud DNS change only when the record differs.
4. Log every decision.

## Files

- `app/dns_updater.py` - updater logic
- `app/requirements.txt` - Python dependencies
- `Dockerfile` - container image
- `k8s/configmap.yaml` - record list and IP source config
- `k8s/cronjob.yaml` - Kubernetes CronJob, scheduled every 15 minutes
- `k8s/namespace.yaml` - isolated namespace
- `k8s/kustomization.yaml` - apply entrypoint

## Configuration

Records live in `k8s/configmap.yaml`:

```yaml
records:
  - name: one.kaut.io.
    type: A
    zone: kautio
    ttl: 300
```

Add more records by adding entries. For example, to have the updater maintain the apex record for `spillway.trade`:

```yaml
  - name: spillway.trade.
    type: A
    zone: spillway
    ttl: 300
    enabled: true
```

Only `A` records are supported intentionally. CNAMEs do not need dynamic IP updates, and apex records usually cannot be CNAMEs.

## Google Credentials

The CronJob expects this secret:

```text
namespace: dns-updater
secret: google-cloud-dns
key: credentials.json
```

To seed it from the existing Node-RED Google credential secret:

```sh
kubectl create namespace dns-updater --dry-run=client -o yaml | kubectl apply -f -
kubectl -n nodered get secret googlecreds -o jsonpath='{.data.gcreds}' | base64 -d > /tmp/google-cloud-dns.json
kubectl -n dns-updater create secret generic google-cloud-dns \
  --from-file=credentials.json=/tmp/google-cloud-dns.json \
  --dry-run=client -o yaml | kubectl apply -f -
rm /tmp/google-cloud-dns.json
```

The Google service account needs permission to list and change record sets for the target managed zones.

## GHCR Pull Credential

The CronJob runs under the `dns-updater` ServiceAccount, which references an image pull secret named `ghcr-creds`.

To copy the existing FlowMap GHCR pull credential into this namespace:

```sh
kubectl create namespace dns-updater --dry-run=client -o yaml | kubectl apply -f -
kubectl -n flowmap get secret ghcr-creds -o yaml \
  | sed 's/namespace: flowmap/namespace: dns-updater/' \
  | kubectl apply -f -
```

Alternatively, create a fresh GHCR pull secret named `ghcr-creds` in the `dns-updater` namespace.

## IPinfo Token

The updater can use an optional IPinfo token for the `https://ipinfo.io/ip` source. Store it as a Kubernetes secret:

```sh
kubectl -n dns-updater create secret generic ipinfo \
  --from-literal=token='<your-ipinfo-token>' \
  --dry-run=client -o yaml | kubectl apply -f -
```

The CronJob reads that secret as `IPINFO_TOKEN`. If the secret is missing, the updater still runs and falls back to unauthenticated IP sources.

## Build

GitHub Actions publishes the image to GHCR on every push to `main`:

```text
ghcr.io/dukekautington3rd/dns-updater:latest
```

To build it locally:

```sh
cd /Users/lonkaut/iac/kube/dns-updater
docker build -t dns-updater:test .
```

## Deploy

```sh
kubectl apply -k /Users/lonkaut/iac/kube/dns-updater/k8s
```

Create an immediate one-off run:

```sh
kubectl -n dns-updater create job --from=cronjob/dns-updater dns-updater-manual-$(date +%s)
```

Watch logs:

```sh
kubectl -n dns-updater logs job/<job-name>
```
