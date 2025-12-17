# =============================================================================
# AEAD KEY MIGRATION TASK
# =============================================================================
# One-time migration task for re-encrypting deposit account mnemonics
# from the default all-zeros key to the actual key from Secrets Manager.
#
# Usage:
#   1. Build and push the migration image:
#      cd scripts/migrations
#      docker build -t basilica-aead-migration:latest .
#      aws ecr get-login-password --region us-east-2 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-2.amazonaws.com
#      docker tag basilica-aead-migration:latest <account>.dkr.ecr.us-east-2.amazonaws.com/basilica-aead-migration:latest
#      docker push <account>.dkr.ecr.us-east-2.amazonaws.com/basilica-aead-migration:latest
#
#   2. Run DRY RUN first:
#      aws ecs run-task \
#        --cluster basilica-prod-v3-cluster \
#        --task-definition basilica-prod-v3-aead-migration \
#        --launch-type FARGATE \
#        --network-configuration "awsvpcConfiguration={subnets=[<subnet-ids>],securityGroups=[<sg-id>],assignPublicIp=DISABLED}" \
#        --overrides '{"containerOverrides":[{"name":"aead-migration","environment":[{"name":"DRY_RUN","value":"true"}]}]}'
#
#   3. Review CloudWatch logs, then run actual migration with DRY_RUN=false
# =============================================================================

# ECR Repository for migration image
resource "aws_ecr_repository" "aead_migration" {
  name                 = "basilica-aead-migration"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(local.common_tags, {
    Purpose = "AEAD key migration"
  })
}

# Lifecycle policy to keep only recent images
resource "aws_ecr_lifecycle_policy" "aead_migration" {
  repository = aws_ecr_repository.aead_migration.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 5 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

# ECS Task Definition for AEAD Key Migration
module "aead_migration" {
  source = "./modules/ecs-task"

  name_prefix = local.name_prefix
  task_name   = "aead-migration"

  # Container configuration
  container_image = "${aws_ecr_repository.aead_migration.repository_url}:latest"

  # Environment variables
  environment_variables = {
    # Old key is the default all-zeros key that was incorrectly being used
    OLD_AEAD_KEY_HEX = "0000000000000000000000000000000000000000000000000000000000000000"
    # DRY_RUN defaults to true for safety - override to "false" when running actual migration
    DRY_RUN = "true"
    # Database connection string (same database as payments service)
    DATABASE_URL = local.payments_database_url
  }

  # Secrets from AWS Secrets Manager
  secrets = [
    {
      name      = "NEW_AEAD_KEY_HEX"
      valueFrom = aws_secretsmanager_secret.payments_aead_key.arn
    }
  ]

  # Resource allocation - small task, doesn't need much
  cpu    = 256
  memory = 512

  # Logging configuration
  log_retention_days = 30

  # Custom IAM policy for Secrets Manager access
  custom_execution_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [aws_secretsmanager_secret.payments_aead_key.arn]
      }
    ]
  })

  tags = merge(local.common_tags, {
    Purpose = "AEAD key migration"
  })

  depends_on = [module.rds, aws_ecr_repository.aead_migration]
}

# =============================================================================
# OUTPUTS FOR MIGRATION
# =============================================================================

output "aead_migration_task_definition" {
  description = "Task definition ARN for AEAD migration"
  value       = module.aead_migration.task_definition_arn
}

output "aead_migration_ecr_repository" {
  description = "ECR repository URL for migration image"
  value       = aws_ecr_repository.aead_migration.repository_url
}

output "aead_migration_run_command" {
  description = "Command to run the migration task (DRY RUN)"
  value       = <<-EOT
    # DRY RUN (review logs before proceeding):
    aws ecs run-task \
      --cluster ${aws_ecs_cluster.main.name} \
      --task-definition ${module.aead_migration.task_definition_arn} \
      --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=${jsonencode(module.networking.private_subnet_ids)},securityGroups=[${module.networking.ecs_tasks_security_group_id}],assignPublicIp=DISABLED}"

    # ACTUAL MIGRATION (run after reviewing dry run logs):
    aws ecs run-task \
      --cluster ${aws_ecs_cluster.main.name} \
      --task-definition ${module.aead_migration.task_definition_arn} \
      --launch-type FARGATE \
      --network-configuration "awsvpcConfiguration={subnets=${jsonencode(module.networking.private_subnet_ids)},securityGroups=[${module.networking.ecs_tasks_security_group_id}],assignPublicIp=DISABLED}" \
      --overrides '{"containerOverrides":[{"name":"aead-migration","environment":[{"name":"DRY_RUN","value":"false"}]}]}'
  EOT
}
