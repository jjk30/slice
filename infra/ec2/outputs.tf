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
