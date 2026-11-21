# YouTube backend

The Render service runs `yt-dlp` with a Node JavaScript runtime and FFmpeg. Its source repository is https://github.com/vishalnain-10/mediaharbor-youtube-service .

## Deployment

Deploy `backend/Dockerfile` with repository-root build context. Set `MEDIAHARBOR_BACKEND_TOKEN` to a cryptographically random value of at least 32 characters. Do not commit it. Configure these Sites runtime values:

- `YOUTUBE_BACKEND_URL`: HTTPS origin of this backend
- `YOUTUBE_BACKEND_TOKEN`: the matching shared token, stored as a secret

The browser calls only same-origin `/api/youtube`; the key never reaches browser JavaScript. `/health` is public; all extraction, job, and file endpoints require Bearer authorization.

Jobs are queued with one active worker and at most four pending/active jobs. Public completed videos up to four hours and source quality up to 2160p are supported. The service downloads video and audio, remuxes them into MP4, then streams the file with byte-range support. Prepared files and failed partials expire after one hour of inactivity; server restarts discard the job index. Current-track progress is distinguished from overall job completion.

YouTube can block hosting-provider IP addresses. Such responses are reported as extraction errors, never treated as downloadable MP4s. Login-only, age-restricted, private, paid, and live videos are not supported. Refresh the pinned yt-dlp dependency when YouTube changes.

## Verification

```sh
python backend/test_backend.py
node scripts/test-youtube.mjs
```
