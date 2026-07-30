.ONESHELL:
SHELL = bash
SHELLFLAGS = -o pipefail

ENVIRONMENT ?= dev
AWS_REGION ?= $(shell aws configure get region)
AWS_ACCOUNT_ID ?= $(shell aws sts get-caller-identity --output json | jq -r '.Account')
TFSTATE_S3_BUCKET ?= "tfstate-$(AWS_ACCOUNT_ID)"
# The Terraform state bucket is account-wide and physically lives in ONE region
# (whichever env was provisioned first). Use its ACTUAL region for the S3 backend
# — independent of the env's deploy region (AWS_REGION) — so envs in different
# regions share the one state bucket instead of hitting a 301 region mismatch.
# If the bucket doesn't exist yet, fall back to AWS_REGION so the first provision
# creates it there (that becomes the state "home" region).
TFSTATE_REGION ?= $(shell loc=$$(aws s3api get-bucket-location --bucket tfstate-$(AWS_ACCOUNT_ID) --query LocationConstraint --output text 2>/dev/null); if [ -z "$$loc" ]; then echo "$(AWS_REGION)"; elif [ "$$loc" = "None" ] || [ "$$loc" = "null" ]; then echo "us-east-1"; else echo "$$loc"; fi)
TFSTATE_DDB_TABLE ?= "tfstate-lock"
VAR_FILE := $(CURDIR)/00.global/vars/$(ENVIRONMENT).tfvars

TF_VAR_tfstate_region := $(TFSTATE_REGION)
export TF_VAR_tfstate_region

MODULES = $(shell find . -type f -name "backend.tf" -exec dirname {} \; | sort -u )

ifeq ($(AUTO_APPROVE), true)
	TF_AUTO_APPROVE := "-auto-approve"
else
	TF_AUTO_APPROVE := ""
endif

define execute_terraform
	set -o pipefail; \
	terraform -chdir=$(1) $(2) $(3) \
		-input=false \
		-lock=true \
		-compact-warnings \
		-var-file=$(VAR_FILE) \
		 2>&1 | tee -a tf-logs/$(notdir $(1))-$(2).log; \
	exit_code=$$?; \
	if [ $$exit_code -ne 0 ]; then \
		echo "Terraform $(2) failed for module $(1)"; \
		exit $$exit_code; \
	fi
endef

.PHONY: print-modules clean check-env bootstrap init-all plan-all apply-all destroy-all init refresh plan apply destroy

print-modules:
	@for m in $(MODULES); do echo $$m; done

clean:
	@find . -type d -name ".terraform" -prune -exec rm -rf {} \;
	@find . -type f -name ".terraform.lock.hcl" -prune -exec rm -f {} \;

check-env:
	@if [ -z $(AWS_REGION) ]; then \
		echo "AWS_REGION was not set."; \
		exit 1; \
	 fi
	@if [ -z $(TFSTATE_REGION) ]; then \
		echo "TFSTATE_REGION was not set."; \
		exit 1; \
	 fi
	@if [ ! -f $(VAR_FILE) ]; then \
  		echo "VAR_FILE: $(VAR_FILE) does not exist."; \
  	fi

	@mkdir -p tf-logs

bootstrap: check-env
	@echo "Bootstrapping Terraform: S3 Bucket: $(TFSTATE_S3_BUCKET), DynamoDB Table: $(TFSTATE_DDB_TABLE)"
	@echo "Checking if S3 Bucket $(TFSTATE_S3_BUCKET) exists"
	@if ! aws s3api head-bucket --region $(TFSTATE_REGION) --bucket $(TFSTATE_S3_BUCKET) > /dev/null 2>&1; then \
		echo "S3 Bucket $(TFSTATE_S3_BUCKET) does not exists, creating..."; \
		aws s3 mb --region $(TFSTATE_REGION) s3://$(TFSTATE_S3_BUCKET) > /dev/null 2>&1; \
		aws s3api put-bucket-ownership-controls --region $(TFSTATE_REGION) --bucket $(TFSTATE_S3_BUCKET) --ownership-controls Rules="[{ObjectOwnership=BucketOwnerPreferred}]" > /dev/null 2>&1; \
		aws s3api put-bucket-acl --region $(TFSTATE_REGION) --bucket $(TFSTATE_S3_BUCKET) --acl private > /dev/null 2>&1; \
		aws s3api put-public-access-block --bucket $(TFSTATE_S3_BUCKET) --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" > /dev/null 2>&1; \
		aws s3api put-bucket-versioning --region $(TFSTATE_REGION) --bucket $(TFSTATE_S3_BUCKET) --versioning-configuration Status=Enabled > /dev/null 2>&1; \
		aws s3api put-bucket-encryption --region $(TFSTATE_REGION) --bucket $(TFSTATE_S3_BUCKET) --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"},"BucketKeyEnabled":true}]}' > /dev/null 2>&1; \
		bkt=$(TFSTATE_S3_BUCKET); \
		aws s3api put-bucket-policy --region $(TFSTATE_REGION) --bucket "$$bkt" \
			--policy "$$(printf '{"Version":"2012-10-17","Statement":[{"Sid":"DenyInsecureTransport","Effect":"Deny","Principal":"*","Action":"s3:*","Resource":["arn:aws:s3:::%s","arn:aws:s3:::%s/*"],"Condition":{"Bool":{"aws:SecureTransport":"false"}}}]}' "$$bkt" "$$bkt")" > /dev/null 2>&1; \
		echo "Created S3 Bucket $(TFSTATE_S3_BUCKET)."; \
	 else \
		echo "S3 Bucket $(TFSTATE_S3_BUCKET) exists."; \
	 fi
	@echo "Checking if DynamoDB table $(TFSTATE_DDB_TABLE) exists"
	@if ! aws dynamodb describe-table --region $(TFSTATE_REGION) --table-name $(TFSTATE_DDB_TABLE) > /dev/null 2>&1 ; then \
		echo "DynamoDB table $(TFSTATE_DDB_TABLE) does not exists, creating..."; \
		aws dynamodb create-table \
          	--region $(TFSTATE_REGION) \
          	--table-name $(TFSTATE_DDB_TABLE) \
          	--attribute-definitions AttributeName=LockID,AttributeType=S \
          	--key-schema AttributeName=LockID,KeyType=HASH \
          	--billing-mode PAY_PER_REQUEST > /dev/null 2>&1 ; \
		echo "Created DynamoDB table $(TFSTATE_DDB_TABLE)."; \
	 else \
		echo "DynamoDB table $(TFSTATE_DDB_TABLE) exists."; \
	 fi

init-all:
	@for m in $(MODULES); do \
  		$(MAKE) init MODULE=$$m || exit 1; \
  	done

plan-all:
	@for m in $(MODULES); do \
  		$(MAKE) plan MODULE=$$m || exit 1; \
  	done

apply-all:
	@for m in $(MODULES); do \
  		$(MAKE) apply MODULE=$$m || exit 1; \
  	done

destroy-all: MODULES := $(shell find . -type f -name "backend.tf" -exec dirname {} \; | sort -r )
destroy-all:
	@for m in $(MODULES); do \
  		$(MAKE) destroy MODULE=$$m || exit 1; \
  	done


init: check-env
	@if [ -z $(MODULE) ]; then \
		echo "MODULE was not set."; \
		exit 1; \
	fi
	@rm -rf $(MODULE)/.terraform/*.tfstate
	@echo ENVIRONMENT=$(ENVIRONMENT) MODULE=$(MODULE) terraform::init
	@terraform -chdir=$(MODULE) init \
		-input=false \
		-backend=true \
		-backend-config="region=$(TFSTATE_REGION)" \
		-backend-config="bucket=$(TFSTATE_S3_BUCKET)" \
		-backend-config="dynamodb_table=$(TFSTATE_DDB_TABLE)" \
		2>&1 | tee -a tf-logs/$(notdir $(MODULE))-init.log

tf-select-ws:
	@if [ -z $(MODULE) ]; then \
		echo "MODULE was not set."; \
		exit 1; \
	fi
	@echo ENVIRONMENT=$(ENVIRONMENT) MODULE=$(MODULE) Switching to Terraform workspace: $(ENVIRONMENT)
	@terraform -chdir=$(MODULE) workspace select -or-create=true $(ENVIRONMENT)

refresh: init tf-select-ws
	@echo ENVIRONMENT=$(ENVIRONMENT) MODULE=$(MODULE) terraform::refresh
	@$(call execute_terraform,$(MODULE),refresh)

plan: init tf-select-ws
	@echo ENVIRONMENT=$(ENVIRONMENT) MODULE=$(MODULE) terraform::plan
	@$(call execute_terraform,$(MODULE),plan)

# First-apply bootstrap for EKS stages: create module.eks BEFORE the
# kubernetes/helm/kubectl providers configure. Those providers point at
# module.eks.cluster_endpoint, which isn't known until the cluster exists, so on
# a brand-new cluster the alekc/kubectl provider fails with "invalid provider
# configuration: no configuration has been provided". Targeting module.eks first
# creates the cluster so the providers resolve. Runs only when module.eks isn't
# in state yet (the very first apply); it's a no-op on every apply after that.
apply: init tf-select-ws
	@echo ENVIRONMENT=$(ENVIRONMENT) MODULE=$(MODULE) terraform::apply
	@if grep -rqsE '^[[:space:]]*module[[:space:]]+"eks"' $(MODULE)/*.tf 2>/dev/null && ! terraform -chdir=$(MODULE) state list 2>/dev/null | grep -q '^module\.eks'; then \
		echo ">> First apply: creating the EKS cluster (module.eks) first so the Kubernetes providers can configure"; \
		terraform -chdir=$(MODULE) apply -target=module.eks -input=false -lock=true -auto-approve -var-file=$(VAR_FILE) 2>&1 | tee -a tf-logs/$(notdir $(MODULE))-apply.log || { echo "targeted module.eks apply failed for $(MODULE)"; exit 1; }; \
	fi
	@$(call execute_terraform,$(MODULE),apply,"-auto-approve")

destroy: init tf-select-ws
	@echo ENVIRONMENT=$(ENVIRONMENT) MODULE=$(MODULE) terraform::destroy
	@$(call execute_terraform,$(MODULE),destroy,"-auto-approve")
