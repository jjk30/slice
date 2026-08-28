output "elastic_ip" {
  description = "Static public IP of the instance (target of the api A record)."
  value       = aws_eip.api.public_ip
}

output "route53_nameservers" {
  description = "Nameservers for the recreated hosted zone. Point the registrar (Namecheap) at these to move DNS authority to this stack."
  value       = aws_route53_zone.main.name_servers
}

output "instance_id" {
  description = "EC2 instance id (use with SSM Session Manager to connect)."
  value       = aws_instance.app.id
}

output "grafana_url" {
  description = "Phase 17: Grafana URL (behind Caddy; admin password from the GRAFANA_ADMIN_PASSWORD key in the app secret)."
  value       = "https://${var.grafana_subdomain}"
}

output "config_bucket" {
  description = "Private S3 bucket holding the box's stack config (docker-compose.yml, Caddyfile, ...). Used by the website deploy to sync the updated Caddyfile/compose, and as the transfer bucket when the repo is private."
  value       = aws_s3_bucket.config.bucket
}

output "website_url" {
  description = "Public URL of the marketing site once the root A record and Caddy site block are live."
  value       = "https://${var.domain_name}"
}

output "backup_bucket" {
  description = "Private S3 bucket that the nightly Postgres backup script uploads slice-YYYY-MM-DD.dump into. Use this name in the cron line's SLICE_BACKUP_BUCKET."
  value       = aws_s3_bucket.backups.bucket
}
