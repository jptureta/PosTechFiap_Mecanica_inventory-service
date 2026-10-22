terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

resource "aws_db_subnet_group" "service" {
  name       = "oficina-inventory-${var.environment}"
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "service_db" {
  name        = "oficina-inventory-db-${var.environment}"
  description = "PostgreSQL access from the EKS node security group"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.eks_cluster_security_group_id]
  }
}

resource "aws_db_instance" "service" {
  identifier                 = "oficina-inventory-${var.environment}"
  engine                     = "postgres"
  engine_version             = "16"
  instance_class             = var.db_instance_class
  allocated_storage          = 20
  max_allocated_storage      = 100
  storage_type               = "gp3"
  db_name                    = "inventory_service"
  username                   = "inventory_service"
  password                   = var.db_password
  db_subnet_group_name       = aws_db_subnet_group.service.name
  vpc_security_group_ids     = [aws_security_group.service_db.id]
  publicly_accessible        = false
  storage_encrypted          = true
  backup_retention_period    = 7
  deletion_protection        = var.environment == "production"
  skip_final_snapshot        = var.environment != "production"
  final_snapshot_identifier  = var.environment == "production" ? "oficina-inventory-production-final" : null
  auto_minor_version_upgrade = true
}
