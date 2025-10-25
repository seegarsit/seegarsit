# Render Deployment Guide

Follow these steps to ensure the SQLite database persists between deploys and restarts when running on Render:

1. **Attach a persistent disk**
   - In the Render dashboard, open your Web Service.
   - Add a Disk with enough space for ticket data (1–5 GB is usually sufficient).
2. **Redeploy the service**
   - Trigger a redeploy so Render mounts the disk and provides the `RENDER_DATA_DIR` environment variable (e.g., `/var/data`).
3. **(Optional) Set an explicit database URL**
   - In the service settings, add the environment variable `TICKETS_DB` with the value `sqlite:////var/data/tickets.db` to pin the database location.

After the disk is attached and the service has restarted, existing tickets and feedback will be kept on the persistent volume.
