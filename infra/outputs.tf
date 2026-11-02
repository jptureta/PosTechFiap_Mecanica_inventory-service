output "database_endpoint" {
  value = aws_db_instance.service.endpoint
}

output "database_security_group_id" {
  value = aws_security_group.service_db.id
}

output "inventory_commands_queue_url" {
  value = aws_sqs_queue.inventory_commands.url
}

output "inventory_commands_dlq_url" {
  value = aws_sqs_queue.inventory_commands_dlq.url
}
