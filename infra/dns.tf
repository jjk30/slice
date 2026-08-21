# ---------------------------------------------------------------------------
# Route 53 hosted zone for the apex domain.
# Creating this zone allocates a set of AWS nameservers (see the
# route53_nameservers output). Those must be pasted into Namecheap to move DNS
# authority to Route 53 — until that switch propagates, the ACM validation
# below cannot resolve.
# ---------------------------------------------------------------------------
resource "aws_route53_zone" "main" {
  name = var.domain_name
}

# ---------------------------------------------------------------------------
# ACM certificate covering the apex and the api subdomain.
#   Primary domain:          sliceapp.dev
#   Subject Alternative Name: api.sliceapp.dev
# One cert therefore terminates TLS for both. Must live in the same region as
# the ALB (us-east-1) — this whole config is us-east-1, so we're good.
# ---------------------------------------------------------------------------
resource "aws_acm_certificate" "main" {
  domain_name               = var.domain_name
  subject_alternative_names = [var.api_subdomain]
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# ---------------------------------------------------------------------------
# DNS validation records: one CNAME per distinct domain on the cert, created
# automatically in the hosted zone. for_each keys by domain name so apex and
# subdomain each get their record (and duplicate validation options dedupe).
# ---------------------------------------------------------------------------
resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.main.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      type   = dvo.resource_record_type
      record = dvo.resource_record_value
    }
  }

  zone_id         = aws_route53_zone.main.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

# ---------------------------------------------------------------------------
# Waits for ACM to see the validation records and issue the cert.
# NOTE: `terraform apply` WILL BLOCK here until the Namecheap nameserver switch
# to the Route 53 nameservers has propagated and ACM can resolve the CNAMEs
# above. That hang is expected — the zone must exist and its nameservers be
# live at the registrar before this can complete. We manage that ordering
# manually (apply, grab nameservers, set them at Namecheap, let apply finish).
# ---------------------------------------------------------------------------
resource "aws_acm_certificate_validation" "main" {
  certificate_arn         = aws_acm_certificate.main.arn
  validation_record_fqdns = [for record in aws_route53_record.cert_validation : record.fqdn]
}

# ---------------------------------------------------------------------------
# HTTPS listener on the ALB (port 443). Terminates TLS with the ACM cert and
# forwards to the same gateway target group the HTTP listener used to serve.
# certificate_arn references the *validated* ARN (via the validation resource,
# not aws_acm_certificate.main.arn directly) so this listener can't be created
# before the cert is actually issued.
# ---------------------------------------------------------------------------
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.main.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
}

# ---------------------------------------------------------------------------
# api.sliceapp.dev → ALB. An alias A-record (not a CNAME) is the correct way to
# point a hostname at an ALB: it resolves to the ALB's addresses at query time
# and works at both apex and subdomain.
# ---------------------------------------------------------------------------
resource "aws_route53_record" "api" {
  zone_id = aws_route53_zone.main.zone_id
  name    = var.api_subdomain
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

# ---------------------------------------------------------------------------
# Apex sliceapp.dev → same ALB, so the bare domain also resolves. Apex records
# can't be CNAMEs, which is exactly why the Route 53 alias A-record exists.
# ---------------------------------------------------------------------------
resource "aws_route53_record" "apex" {
  zone_id = aws_route53_zone.main.zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
