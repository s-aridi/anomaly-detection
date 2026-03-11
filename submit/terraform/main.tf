terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

data "aws_ami" "ubuntu_2404" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

locals {
  project_name = "anomaly-detection"
  bucket_name  = "${local.project_name}-${random_id.bucket_suffix.hex}"
}

variable "aws_region" {
  type        = string
  description = "AWS region"
  default     = "us-east-1"
}

variable "my_ip" {
  type        = string
  description = "Your public IP in CIDR format for SSH"
  default     = "199.111.224.31/32"
}

variable "key_name" {
  type        = string
  description = "Existing EC2 key pair name"
  default     = "ds5220"
}

variable "repo_url" {
  type        = string
  description = "URL of your forked anomaly-detection repo"
  default     = "https://github.com/s-aridi/anomaly-detection.git"
}

variable "vpc_id" {
  type        = string
  description = "VPC ID"
  default     = "vpc-09c114d2b8b720c9f"
}

variable "subnet_id" {
  type        = string
  description = "Public subnet ID"
  default     = "subnet-05b3b22aa77d3692e"
}

resource "aws_sns_topic" "app" {
  name = "ds5220-dp1"
}

resource "aws_sns_topic_policy" "app" {
  arn = aws_sns_topic.app.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowS3Publish"
        Effect    = "Allow"
        Principal = { Service = "s3.amazonaws.com" }
        Action    = "sns:Publish"
        Resource  = aws_sns_topic.app.arn
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "aws:SourceArn" = aws_s3_bucket.app.arn
          }
        }
      }
    ]
  })
}

resource "aws_s3_bucket" "app" {
  bucket        = local.bucket_name
  force_destroy = true

  tags = {
    Name = local.project_name
  }
}

resource "aws_s3_bucket_notification" "app" {
  bucket = aws_s3_bucket.app.id

  topic {
    topic_arn     = aws_sns_topic.app.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "raw/"
    filter_suffix = ".csv"
  }

  depends_on = [aws_sns_topic_policy.app]
}

resource "aws_iam_role" "ec2" {
  name = "${local.project_name}-ec2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "bucket_access" {
  name = "${local.project_name}-bucket-access"
  role = aws_iam_role.ec2.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.app.arn
      },
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject"
        ]
        Resource = "${aws_s3_bucket.app.arn}/*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "ec2" {
  name = "${local.project_name}-instance-profile"
  role = aws_iam_role.ec2.name
}

resource "aws_security_group" "app" {
  name        = "${local.project_name}-sg"
  description = "Allow SSH from my IP and FastAPI from anywhere"
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH from my IP"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip]
  }

  ingress {
    description = "FastAPI from anywhere"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.project_name}-sg"
  }
}

resource "aws_instance" "app" {
  ami                         = data.aws_ami.ubuntu_2404.id
  instance_type               = "t3.micro"
  key_name                    = var.key_name
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.app.id]
  iam_instance_profile        = aws_iam_instance_profile.ec2.name
  associate_public_ip_address = true

  root_block_device {
    volume_size           = 16
    volume_type           = "gp3"
    delete_on_termination = true
  }

  user_data = <<-EOF
              #!/bin/bash
              set -euxo pipefail

              apt-get update -y
              apt-get install -y git python3 python3-pip python3-venv

              cd /opt
              if [ ! -d /opt/anomaly-detection ]; then
                git clone ${var.repo_url} /opt/anomaly-detection
              fi

              cd /opt/anomaly-detection
              python3 -m venv venv
              . venv/bin/activate
              pip install --upgrade pip
              pip install -r requirements.txt

              echo "BUCKET_NAME=${aws_s3_bucket.app.bucket}" > /etc/environment
              export BUCKET_NAME=${aws_s3_bucket.app.bucket}

              touch /opt/anomaly-detection/app.log
              chmod 666 /opt/anomaly-detection/app.log
              chown -R ubuntu:ubuntu /opt/anomaly-detection

              cat > /etc/systemd/system/anomaly-api.service <<SYSTEMD
              [Unit]
              Description=Anomaly Detection FastAPI Service
              After=network.target

              [Service]
              User=ubuntu
              WorkingDirectory=/opt/anomaly-detection
              Environment=BUCKET_NAME=${aws_s3_bucket.app.bucket}
              ExecStart=/opt/anomaly-detection/venv/bin/fastapi run app.py --host 0.0.0.0 --port 8000
              Restart=always

              [Install]
              WantedBy=multi-user.target
              SYSTEMD

              systemctl daemon-reload
              systemctl enable anomaly-api
              systemctl start anomaly-api
              EOF

  depends_on = [
    aws_iam_role_policy.bucket_access
  ]

  tags = {
    Name = "${local.project_name}-ec2"
  }
}

resource "aws_eip" "app" {
  domain = "vpc"

  tags = {
    Name = "${local.project_name}-eip"
  }
}

resource "aws_eip_association" "app" {
  instance_id   = aws_instance.app.id
  allocation_id = aws_eip.app.id
}

resource "aws_sns_topic_subscription" "http" {
  topic_arn = aws_sns_topic.app.arn
  protocol  = "http"
  endpoint  = "http://${aws_eip.app.public_ip}:8000/notify"

  depends_on = [aws_eip_association.app]
}

output "bucket_name" {
  value = aws_s3_bucket.app.bucket
}

output "instance_id" {
  value = aws_instance.app.id
}

output "elastic_ip" {
  value = aws_eip.app.public_ip
}

output "api_url" {
  value = "http://${aws_eip.app.public_ip}:8000"
}

output "notify_endpoint" {
  value = "http://${aws_eip.app.public_ip}:8000/notify"
}

output "topic_arn" {
  value = aws_sns_topic.app.arn
}
