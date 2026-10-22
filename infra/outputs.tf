output "database_endpoint" {
  value = aws_db_instance.service.endpoint
}

output "database_security_group_id" {
  value = aws_security_group.service_db.id
}
