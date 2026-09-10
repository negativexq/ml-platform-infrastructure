output "vpc_id" {
  description = "VPC id."
  value       = aws_vpc.this.id
}

output "public_subnet_ids" {
  description = "Public subnet ids, for internet-facing load balancers."
  value       = [for s in aws_subnet.public : s.id]
}

output "private_subnet_ids" {
  description = "Private subnet ids, where nodes and RDS live."
  value       = [for s in aws_subnet.private : s.id]
}

output "vpc_cidr_block" {
  description = "VPC CIDR, for security group rules."
  value       = aws_vpc.this.cidr_block
}
