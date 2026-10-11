SiftNodes persistent bot update

1) Stop the old bot process before replacing bot.py.
2) Back up your existing bot.py AND primecloud.db first.
3) Replace bot.py with this file. Keep the existing primecloud.db in the same working directory; it contains VPS records, users, settings, and admin data.
4) Keep Docker Engine installed and enabled at boot:
   sudo systemctl enable --now docker
5) Start the bot with the same Python environment and token configuration as before.

Persistence notes:
- This update does not delete or rebuild existing VPS containers.
- Containers created by the bot use Docker restart policy unless-stopped, so they can restart after host reboot.
- Existing container writable layers survive a normal bot restart / VPS reboot as long as Docker's data directory and containers are not deleted.
- This is not protection against disk failure, VPS provider wiping/replacing the disk, `docker system prune`/container removal, or deleting primecloud.db. Keep off-server backups of primecloud.db and critical VPS data.
- Database path is controlled by PRIME_DB and defaults to primecloud.db. Keep the same path and file when updating.

Admin:
- Discord user ID 1507714909977313402 is inserted into the admins table at startup.
- Existing owner/admin configuration remains intact.

Presence:
- DND with rotating custom status every 20 seconds; no Playing activity.
- VPS count reports running containers carrying the bot's VPS label.
