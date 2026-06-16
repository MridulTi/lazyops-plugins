#!/usr/bin/env bash
#
# Read Auto Scaling group names from a file, resolve the AMI from each group's
# active launch template, deduplicate AMIs, and start SSM Automation executions
# (fire-and-forget — does not wait for completion).
#
# Instance-related parameters (InstanceType, SubnetId, SecurityGroupIds,
# IamInstanceProfileName) are read from each ASG and its launch template(s),
# unless overridden via environment variables.
#
# Usage:
#   export AWS_PROFILE=... AWS_REGION=ap-south-1   # or use --region
#   export SSM_AUTOMATION_DOCUMENT="YourCustomDocumentName"
#   ./batch_start_linux_ami_patch_automation.sh path/to/asg-names.txt
#
# Required env:
#   SSM_AUTOMATION_DOCUMENT  Name of the SSM Automation document (same account/region).
#
# Optional env (override ASG/launch-template discovery):
#   AUTOMATION_ASSUME_ROLE_ARN
#   IAM_INSTANCE_PROFILE_NAME
#   INCLUDE_PACKAGES
#   INSTANCE_TYPE
#   SUBNET_ID
#   SECURITY_GROUP_IDS       Comma-separated (no spaces)
#   TARGET_AMI_NAME          Optional; omit to use the document default
#   PRE_UPDATE_SCRIPT
#   POST_UPDATE_SCRIPT
#

set -uo pipefail

# ---------------------------------------------------------------------------
# Defaults (override via environment). No hardcoded account/subnet/SG/instance.
# ---------------------------------------------------------------------------
: "${SSM_AUTOMATION_DOCUMENT:?export SSM_AUTOMATION_DOCUMENT to your automation document name}"

AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
if [[ -z "$AWS_REGION" ]]; then
  AWS_REGION="$(aws configure get region 2>/dev/null || true)"
fi
# trim whitespace (bad copy/paste in env files breaks --region)
AWS_REGION="${AWS_REGION#"${AWS_REGION%%[![:space:]]*}"}"
AWS_REGION="${AWS_REGION%"${AWS_REGION##*[![:space:]]}"}"
if [[ -n "$AWS_REGION" ]]; then
  export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"
else
  echo "Warning: AWS_REGION is not set and no default region in AWS profile." >&2
fi

AUTOMATION_ASSUME_ROLE_ARN="${AUTOMATION_ASSUME_ROLE_ARN:-arn:aws:iam::471112548387:role/ssmPatchRole}"
IAM_INSTANCE_PROFILE_NAME="${IAM_INSTANCE_PROFILE_NAME:-}"
INCLUDE_PACKAGES="${INCLUDE_PACKAGES:-all}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t4g.medium}"
SUBNET_ID="${SUBNET_ID:-}"
SECURITY_GROUP_IDS="${SECURITY_GROUP_IDS:-}"
TARGET_AMI_NAME="${TARGET_AMI_NAME:-}"
PRE_UPDATE_SCRIPT="${PRE_UPDATE_SCRIPT:-none}"
POST_UPDATE_SCRIPT="${POST_UPDATE_SCRIPT:-none}"

# StringMap: single-line JSON string (passed through to the document as StringMap)
METADATA_OPTIONS_JSON="${METADATA_OPTIONS_JSON:-{\"HttpEndpoint\":\"enabled\",\"HttpTokens\":\"required\",\"HttpPutResponseHopLimit\":2}}"

EXCLUDE_PACKAGES="${EXCLUDE_PACKAGES:-openjdk*,telegraf,kibana,*corretto*,nodejs,elasticsearch,redis*,kafka,logstash,bind9,filebeat,docker*,containerd,nginx*,mysql*,mariadb*,postgresql*,tomcat*,jenkins}"

# ---------------------------------------------------------------------------

die() { echo "$*" >&2; exit 1; }

ASG_FILE="${1:-}"
[[ -n "$ASG_FILE" ]] || die "Usage: $0 <asg-names.txt>"
[[ -f "$ASG_FILE" ]] || die "File not found: $ASG_FILE"

command -v aws >/dev/null 2>&1 || die "aws CLI not found"
command -v jq >/dev/null 2>&1 || die "jq is required (brew install jq / apt install jq)"

effective_region() {
  echo "${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || echo unknown)}}"
}

# Global --region must follow `aws` (required for AWS CLI v1).
aws_cli() {
  local r
  r="$(effective_region)"
  if [[ -n "$r" && "$r" != "unknown" ]]; then
    aws --region "$r" "$@"
  else
    aws "$@"
  fi
}

aws_cli_err() {
  aws_cli "$@" 2>&1
}

# First non-empty subnet from ASG VPCZoneIdentifier (comma-separated).
first_subnet_from_asg_json() {
  echo "$1" | jq -r '
    .AutoScalingGroups[0].VPCZoneIdentifier
    | split(",")
    | map(gsub("^\\s+|\\s+$"; ""))
    | map(select(length > 0))
    | .[0] // empty
  '
}

# Instance type from MixedInstancesPolicy override row matching LaunchTemplateId (optional).
instance_type_from_mip_override() {
  local asg_json="$1" lt_id="$2"
  echo "$asg_json" | jq -r --arg id "$lt_id" '
    .AutoScalingGroups[0].MixedInstancesPolicy.Overrides[]?
    | select(.LaunchTemplateSpecification.LaunchTemplateId == $id)
    | .InstanceType // empty
    ' | head -n1
}

# From one launch-template version JSON: ImageId, InstanceType, SGs, IAM profile name.
# Security groups: LaunchTemplateData.SecurityGroupIds, else union of NetworkInterfaces[].Groups.
_ami_from_lt_version() {
  local id="$1" ver="$2"
  [[ -z "$ver" || "$ver" == "null" ]] && ver='$Latest'
  aws_cli ec2 describe-launch-template-versions \
    --launch-template-id "$id" \
    --versions "$ver" \
    --output json 2>/dev/null
}

_resolve_launch_template_id() {
  local lt_id="$1" lt_name="$2"
  if [[ -n "$lt_id" && "$lt_id" != "null" ]]; then
    echo "$lt_id"
    return
  fi
  if [[ -n "$lt_name" && "$lt_name" != "null" ]]; then
    aws_cli ec2 describe-launch-templates \
      --launch-template-names "$lt_name" \
      --query 'LaunchTemplates[0].LaunchTemplateId' \
      --output text 2>/dev/null
  fi
}

_image_id_from_lt_json() {
  local lt_json="$1"
  echo "$lt_json" | jq -r '.LaunchTemplateVersions[0].LaunchTemplateData.ImageId // empty'
}

# Fallback: fields from first InService instance in the ASG (ami, type, subnet, sgs, iam).
_instance_fields_from_asg() {
  local asg_json="$1"
  local instance_id
  instance_id=$(echo "$asg_json" | jq -r '
    .AutoScalingGroups[0].Instances[]?
    | select(.LifecycleState == "InService")
    | .InstanceId
  ' | head -n1)
  [[ -z "$instance_id" || "$instance_id" == "null" ]] && return 1

  aws_cli ec2 describe-instances \
    --instance-ids "$instance_id" \
    --output json 2>/dev/null | jq -r '
    .Reservations[0].Instances[0] as $i |
    if $i == null then empty else
      [
        ($i.ImageId // empty),
        ($i.InstanceType // empty),
        ($i.SubnetId // empty),
        ([$i.SecurityGroups[]?.GroupId] | join(",")),
        (
          ($i.IamInstanceProfile.Arn // "" | if length > 0 then split("/") | .[-1] else empty end)
        )
      ] | @tsv
    end
  '
}

_ami_from_asg_instances() {
  local asg_json="$1"
  _instance_fields_from_asg "$asg_json" | awk -F'\t' '{print $1}'
}

# Fill empty discovered fields from running instance (LT/ASG values win when present).
_merge_with_instance_fallback() {
  local asg_json="$1" ami="$2" itype="$3" sub="$4" sg="$5" prof="$6"
  local inst_row inst_ami inst_itype inst_sub inst_sg inst_prof
  inst_row=$(_instance_fields_from_asg "$asg_json" 2>/dev/null || true)
  if [[ -n "$inst_row" ]]; then
    IFS=$'\t' read -r inst_ami inst_itype inst_sub inst_sg inst_prof <<<"$inst_row"
    [[ -z "$ami" || "$ami" == "None" ]] && ami="$inst_ami"
    [[ -z "$itype" || "$itype" == "null" ]] && itype="$inst_itype"
    [[ -z "$sub" || "$sub" == "null" ]] && sub="$inst_sub"
    [[ -z "$sg" || "$sg" == "null" ]] && sg="$inst_sg"
    [[ -z "$prof" || "$prof" == "null" ]] && prof="$inst_prof"
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$ami" "$itype" "$sub" "$sg" "$prof"
}

# Legacy launch configuration (no launch template on ASG).
_fields_from_launch_configuration() {
  local lc_name="$1"
  local lc_json
  lc_json=$(aws_cli autoscaling describe-launch-configurations \
    --launch-configuration-names "$lc_name" \
    --output json 2>/dev/null) || return 1
  echo "$lc_json" | jq -r '
    .LaunchConfigurations[0] as $lc |
    if $lc == null then empty else
      [
        ($lc.ImageId // empty),
        ($lc.InstanceType // empty),
        (($lc.SecurityGroups // []) | join(",")),
        ($lc.IamInstanceProfile // empty)
      ] | @tsv
    end
  '
}

diagnose_asg_ami_failure() {
  local asg="$1"
  local region lc_name lt_id lt_name lt_ver img n inst_count

  region="$(effective_region)"
  echo "  Diagnostic (region=$region):"

  local json
  json=$(aws_cli autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$asg" \
    --output json 2>/dev/null) || {
    echo "  - describe-auto-scaling-groups API call failed"
    return
  }

  n=$(echo "$json" | jq '.AutoScalingGroups | length')
  if [[ "$n" -eq 0 ]]; then
    echo "  - ASG not found in $region (set export AWS_REGION=ap-south-1 or fix name/casing)"
    return
  fi

  lc_name=$(echo "$json" | jq -r '.AutoScalingGroups[0].LaunchConfigurationName // empty')
  lt_id=$(echo "$json" | jq -r '.AutoScalingGroups[0].LaunchTemplate.LaunchTemplateId // empty')
  lt_name=$(echo "$json" | jq -r '.AutoScalingGroups[0].LaunchTemplate.LaunchTemplateName // empty')
  lt_ver=$(echo "$json" | jq -r '.AutoScalingGroups[0].LaunchTemplate.Version // empty')

  if [[ -z "$lt_id" || "$lt_id" == "null" ]]; then
    lt_id=$(echo "$json" | jq -r '.AutoScalingGroups[0].MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.LaunchTemplateId // empty')
    lt_ver=$(echo "$json" | jq -r '.AutoScalingGroups[0].MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.Version // empty')
    [[ -z "$lt_name" || "$lt_name" == "null" ]] && lt_name=$(echo "$json" | jq -r '.AutoScalingGroups[0].MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.LaunchTemplateName // empty')
  fi

  if [[ -n "$lc_name" && "$lc_name" != "null" ]]; then
    img=$(_fields_from_launch_configuration "$lc_name" | awk -F'\t' '{print $1}')
    echo "  - uses LaunchConfiguration: $lc_name (ImageId=${img:-<empty>})"
  elif [[ -n "$lt_id" || -n "$lt_name" ]]; then
    lt_id=$(_resolve_launch_template_id "$lt_id" "$lt_name")
    [[ -z "$lt_ver" || "$lt_ver" == "null" ]] && lt_ver='$Latest'
    img=$(_image_id_from_lt_json "$(_ami_from_lt_version "$lt_id" "$lt_ver")")
    echo "  - uses LaunchTemplate: ${lt_id:-?} version ${lt_ver} (ImageId=${img:-<empty>})"
  else
    echo "  - no LaunchTemplate or LaunchConfiguration on ASG"
  fi

  inst_count=$(echo "$json" | jq '[.AutoScalingGroups[0].Instances[]? | select(.LifecycleState=="InService")] | length')
  echo "  - InService instances: $inst_count"
  if [[ "$inst_count" -gt 0 ]]; then
    inst_row=$(_instance_fields_from_asg "$json" 2>/dev/null || true)
    if [[ -n "$inst_row" ]]; then
      IFS=$'\t' read -r img itype inst_sub inst_sg inst_prof <<<"$inst_row"
      echo "  - from running instance: AMI=${img:-?} InstanceType=${itype:-?} SubnetId=${inst_sub:-?} SGs=${inst_sg:-?} IAM=${inst_prof:-?}"
    fi
  fi
}

_extract_from_lt_version_json() {
  local lt_json="$1"

  echo "$lt_json" | jq -r '
    .LaunchTemplateVersions[0].LaunchTemplateData as $d |
    ($d.ImageId // empty) as $ami |
    ($d.InstanceType // empty) as $itype |
    (
      if ($d.SecurityGroupIds // [] | length) > 0 then
        $d.SecurityGroupIds
      else
        [ $d.NetworkInterfaces[]? | .Groups[]? ] | flatten | unique
      end
    ) as $sgs |
    (
      if (($d.IamInstanceProfile // null) | type) == "string" then
        $d.IamInstanceProfile
      elif (($d.IamInstanceProfile // {}) | .Name // empty | length) > 0 then
        $d.IamInstanceProfile.Name
      else
        (($d.IamInstanceProfile // {}) | .Arn // "" | if length > 0 then split("/") | .[-1] else empty end)
      end
    ) as $prof |
    [$ami, $itype, ($sgs | join(",")), $prof] | @tsv
  '
}

# Resolve instance type: launch template first, else MixedInstancesPolicy override for this LT id.
_resolve_instance_type() {
  local asg_json="$1" lt_id="$2" lt_ver="$3" itype_from_lt="$4"
  local mip_type
  if [[ -n "$itype_from_lt" && "$itype_from_lt" != "null" ]]; then
    echo "$itype_from_lt"
    return
  fi
  mip_type=$(instance_type_from_mip_override "$asg_json" "$lt_id")
  if [[ -n "$mip_type" && "$mip_type" != "null" ]]; then
    echo "$mip_type"
    return
  fi
  # Any override row InstanceType (same launch template, type-only overrides).
  mip_type=$(echo "$asg_json" | jq -r '
    .AutoScalingGroups[0].MixedInstancesPolicy.Overrides[]?.InstanceType // empty
  ' | head -n1)
  if [[ -n "$mip_type" && "$mip_type" != "null" ]]; then
    echo "$mip_type"
    return
  fi
  echo ""
}

# For ASG: print lines "ami<TAB>instance_type<TAB>subnet_id<TAB>sg_csv<TAB>iam_profile"
# One line per distinct AMI from primary launch template and MixedInstancesPolicy overrides.
emit_instance_rows_for_asg() {
  local asg="$1"
  local json lt_id lt_ver lt_name lc_name n i ovr_ids ovr_vers itype lt_json row ami_part itype_part sg_part prof_part aws_err

  json=$(aws_cli autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$asg" \
    --output json 2>/dev/null) || {
    aws_err=$(aws_cli_err autoscaling describe-auto-scaling-groups \
      --auto-scaling-group-names "$asg" \
      --output json | tail -n 3)
    echo "$0: describe-auto-scaling-groups failed for: $asg" >&2
    [[ -n "$aws_err" ]] && echo "  AWS error: $aws_err" >&2
    return 1
  }

  local group_count
  group_count=$(echo "$json" | jq '.AutoScalingGroups | length')
  if [[ "$group_count" -eq 0 ]]; then
    echo "$0: no such Auto Scaling group: $asg" >&2
    return 1
  fi

  local subnet
  subnet=$(first_subnet_from_asg_json "$json")
  if [[ -z "$subnet" || "$subnet" == "null" ]]; then
    echo "$0: ASG $asg has no subnets in VPCZoneIdentifier" >&2
    return 1
  fi

  lc_name=$(echo "$json" | jq -r '.AutoScalingGroups[0].LaunchConfigurationName // empty')
  if [[ -n "$lc_name" && "$lc_name" != "null" ]]; then
    row=$(_fields_from_launch_configuration "$lc_name") || {
      echo "$0: describe-launch-configurations failed for $lc_name" >&2
      return 1
    }
    IFS=$'\t' read -r ami_part itype_part sg_part prof_part <<<"$row"
    merged=$(_merge_with_instance_fallback "$json" "$ami_part" "$itype_part" "$subnet" "$sg_part" "$prof_part")
    IFS=$'\t' read -r ami_part itype_part subnet sg_part prof_part <<<"$merged"
  else
    lt_id=$(echo "$json" | jq -r '.AutoScalingGroups[0].LaunchTemplate.LaunchTemplateId // empty')
    lt_name=$(echo "$json" | jq -r '.AutoScalingGroups[0].LaunchTemplate.LaunchTemplateName // empty')
    lt_ver=$(echo "$json" | jq -r '.AutoScalingGroups[0].LaunchTemplate.Version // empty')

    if [[ -z "$lt_id" || "$lt_id" == "null" ]]; then
      lt_id=$(echo "$json" | jq -r '.AutoScalingGroups[0].MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.LaunchTemplateId // empty')
      lt_ver=$(echo "$json" | jq -r '.AutoScalingGroups[0].MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.Version // empty')
      [[ -z "$lt_name" || "$lt_name" == "null" ]] && lt_name=$(echo "$json" | jq -r '.AutoScalingGroups[0].MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.LaunchTemplateName // empty')
    fi

    lt_id=$(_resolve_launch_template_id "$lt_id" "$lt_name")
    if [[ -z "$lt_id" || "$lt_id" == "null" ]]; then
      echo "$0: ASG $asg has no LaunchTemplate / LaunchConfiguration" >&2
      return 1
    fi

    _emit_one_lt() {
      local lid="$1" lver="$2" merged
      [[ -z "$lver" || "$lver" == "null" ]] && lver='$Latest'
      lt_json=$(_ami_from_lt_version "$lid" "$lver")
      if [[ -z "$lt_json" ]]; then
        echo "$0: describe-launch-template-versions failed for $lid (version $lver)" >&2
        return 1
      fi
      row=$(_extract_from_lt_version_json "$lt_json")
      IFS=$'\t' read -r ami_part itype_part sg_part prof_part <<<"$row"
      itype=$(_resolve_instance_type "$json" "$lid" "$lver" "$itype_part")
      merged=$(_merge_with_instance_fallback "$json" "$ami_part" "$itype" "$subnet" "$sg_part" "$prof_part")
      IFS=$'\t' read -r ami_part itype subnet sg_part prof_part <<<"$merged"
      if [[ -z "$ami_part" || "$ami_part" == "None" ]]; then
        echo "$0: no ImageId on LT $lid version $lver and no InService instance in ASG $asg" >&2
        return 0
      fi
      echo "  resolved LT $lid@$lver → AMI=$ami_part InstanceType=$itype SGs=${sg_part:-<empty>} IAM=${prof_part:-<empty>}" >&2
      printf '%s\t%s\t%s\t%s\t%s\n' "$ami_part" "$itype" "$subnet" "$sg_part" "$prof_part"
    }

    _emit_one_lt "$lt_id" "$lt_ver" || return 1

    n=$(echo "$json" | jq '.AutoScalingGroups[0].MixedInstancesPolicy.Overrides // [] | length')
    for ((i = 0; i < n; i++)); do
      ovr_ids=$(echo "$json" | jq -r ".AutoScalingGroups[0].MixedInstancesPolicy.Overrides[$i].LaunchTemplateSpecification.LaunchTemplateId // empty")
      ovr_vers=$(echo "$json" | jq -r ".AutoScalingGroups[0].MixedInstancesPolicy.Overrides[$i].LaunchTemplateSpecification.Version // empty")
      if [[ -n "$ovr_ids" && "$ovr_ids" != "null" ]]; then
        _emit_one_lt "$ovr_ids" "$ovr_vers" || return 1
      fi
    done
    return 0
  fi

  if [[ -z "$ami_part" || "$ami_part" == "None" ]]; then
    return 0
  fi
  echo "  resolved LC $lc_name → AMI=$ami_part InstanceType=$itype_part SGs=${sg_part:-<empty>} IAM=${prof_part:-<empty>}" >&2
  printf '%s\t%s\t%s\t%s\t%s\n' "$ami_part" "$itype_part" "$subnet" "$sg_part" "$prof_part"
}

# Build JSON parameters object for start-automation-execution
build_params_json() {
  local source_ami="$1"
  local inst_type="$2"
  local sub_id="$3"
  local sg_csv="$4"
  local iam_prof="$5"
  local role_arn="$6"
  local sg_array_json
  sg_array_json=$(
    echo "$sg_csv" | tr ',' '\n' |
      sed 's/^[[:space:]]*//;s/[[:space:]]*$//;/^$/d' |
      jq -R . | jq -s .
  )

  jq -n \
    --arg src "$source_ami" \
    --arg role "$role_arn" \
    --arg prof "$iam_prof" \
    --arg inc "$INCLUDE_PACKAGES" \
    --arg inst "$inst_type" \
    --arg sub "$sub_id" \
    --arg name "$TARGET_AMI_NAME" \
    --arg pre "$PRE_UPDATE_SCRIPT" \
    --arg post "$POST_UPDATE_SCRIPT" \
    --arg ex "$EXCLUDE_PACKAGES" \
    --arg meta "$METADATA_OPTIONS_JSON" \
    --argjson sgs "$sg_array_json" \
    '{
      SourceAmiId: [$src],
      AutomationAssumeRole: (if ($role | length) > 0 then [$role] else [] end),
      IamInstanceProfileName: [$prof],
      IncludePackages: [$inc],
      InstanceType: [$inst],
      SubnetId: [$sub],
      SecurityGroupIds: $sgs,
      PreUpdateScript: [$pre],
      PostUpdateScript: [$post],
      ExcludePackages: [$ex],
      MetadataOptions: [$meta]
    }
    + (if ($name | length) > 0 then {TargetAmiName: [$name]} else {} end)'
}

start_one_automation() {
  local ami="$1"
  local inst_type="$2"
  local sub_id="$3"
  local sg_csv="$4"
  local iam_prof="$5"
  local role_arn="$6"
  local params tmp out

  if [[ -z "$role_arn" ]]; then
    echo "  WARNING: AUTOMATION_ASSUME_ROLE_ARN is not set; SSM may reject the execution." >&2
  fi

  params=$(build_params_json "$ami" "$inst_type" "$sub_id" "$sg_csv" "$iam_prof" "$role_arn")
  tmp=$(mktemp)
  echo "$params" >"$tmp"

  echo "Starting automation for AMI=$ami (InstanceType=$inst_type SubnetId=$sub_id) ..."
  if out=$(aws_cli ssm start-automation-execution \
    --document-name "$SSM_AUTOMATION_DOCUMENT" \
    --parameters "file://${tmp}" \
    --output json 2>&1); then
    echo "$out" | jq -r '"  execution: " + .AutomationExecutionId'
  else
    echo "  FAILED: $out" >&2
  fi
  rm -f "$tmp"
}

# Apply env overrides when set; otherwise keep ASG/launch-template discovery.
resolve_instance_fields() {
  local _itype="$1" _sub="$2" _sg="$3" _prof="$4"
  [[ -n "${INSTANCE_TYPE:-}" ]] && _itype="$INSTANCE_TYPE"
  [[ -n "${SUBNET_ID:-}" ]] && _sub="$SUBNET_ID"
  [[ -n "${SECURITY_GROUP_IDS:-}" ]] && _sg="$SECURITY_GROUP_IDS"
  [[ -n "${IAM_INSTANCE_PROFILE_NAME:-}" ]] && _prof="$IAM_INSTANCE_PROFILE_NAME"
  printf '%s\t%s\t%s\t%s\n' "$_itype" "$_sub" "$_sg" "$_prof"
}

missing_required_fields() {
  local _itype="$1" _sub="$2" _sg="$3" _prof="$4"
  local missing=()
  [[ -z "$_itype" ]] && missing+=("INSTANCE_TYPE")
  [[ -z "$_sub" ]] && missing+=("SUBNET_ID")
  [[ -z "$_sg" ]] && missing+=("SECURITY_GROUP_IDS")
  [[ -z "$_prof" ]] && missing+=("IAM_INSTANCE_PROFILE_NAME")
  ((${#missing[@]} > 0)) && printf '%s\n' "${missing[*]}"
}

SEEN_AMIS_FILE=$(mktemp)
trap 'rm -f "$SEEN_AMIS_FILE"' EXIT
STARTED=0

echo "Using AWS region: $(effective_region)${AWS_PROFILE:+ (profile: $AWS_PROFILE)}"

while IFS= read -r line || [[ -n "$line" ]]; do
  # trim and skip blanks / comments
  asg="${line#"${line%%[![:space:]]*}"}"
  asg="${asg%"${asg##*[![:space:]]}"}"
  [[ -z "$asg" || "$asg" =~ ^# ]] && continue

  echo "=== ASG: $asg ==="
  ROWS_TMP=$(mktemp)
  if ! emit_instance_rows_for_asg "$asg" >"$ROWS_TMP"; then
    echo "  (failed to resolve ASG/launch template context; skipping ASG)"
    diagnose_asg_ami_failure "$asg"
    rm -f "$ROWS_TMP"
    continue
  fi
  if [[ ! -s "$ROWS_TMP" ]]; then
    echo "  (no AMI resolved from launch template; skipping ASG)"
    diagnose_asg_ami_failure "$asg"
    rm -f "$ROWS_TMP"
    continue
  fi
  while IFS= read -r _row; do
    [[ -z "$_row" ]] && continue
    IFS=$'\t' read -r ami itype sub_id sg_csv iam_prof <<<"$_row"
    _ov=$(resolve_instance_fields "$itype" "$sub_id" "$sg_csv" "$iam_prof")
    IFS=$'\t' read -r itype sub_id sg_csv iam_prof <<<"$_ov"

    [[ -z "$ami" ]] && continue
    missing=$(missing_required_fields "$itype" "$sub_id" "$sg_csv" "$iam_prof")
    if [[ -n "$missing" ]]; then
      echo "  Skipping AMI $ami — still missing after LT/ASG/instance discovery: $missing"
      echo "    (had InstanceType=$itype SubnetId=$sub_id SGs=${sg_csv:-<empty>} IAM=${iam_prof:-<empty>})"
      continue
    fi
    if grep -Fqx "$ami" "$SEEN_AMIS_FILE" 2>/dev/null; then
      echo "  AMI $ami already scheduled — skip duplicate"
      continue
    fi
    echo "$ami" >>"$SEEN_AMIS_FILE"
    start_one_automation "$ami" "$itype" "$sub_id" "$sg_csv" "$iam_prof" "${AUTOMATION_ASSUME_ROLE_ARN:-}"
    STARTED=$((STARTED + 1))
  done < <(sort -u "$ROWS_TMP")
  rm -f "$ROWS_TMP"
done <"$ASG_FILE"

echo "Done. Started $STARTED unique AMI automation run(s)."
