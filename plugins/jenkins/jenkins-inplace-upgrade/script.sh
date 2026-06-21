#!/bin/bash
set -e

JENKINS_HOME="/var/lib/jenkins"
BACKUP_BASE="/var/backups"
BACKUP_DIR="$BACKUP_BASE/jenkins_upgrade"
LOG_FILE="/var/log/jenkins_upgrade.log"

mkdir -p "$BACKUP_DIR"

echo "========== START ==========" | tee -a $LOG_FILE

# -------------------------------
# STEP 1: CONFIRM
# -------------------------------
read -p "Type YES to continue Jenkins upgrade: " CONFIRM
[[ "$CONFIRM" != "YES" ]] && echo "Aborted." && exit 1

# -------------------------------
# STEP 2: HOT BACKUP (RUNNING)
# -------------------------------
if systemctl is-active --quiet jenkins; then
  if [[ ! -d "$BACKUP_DIR/hot_backup" ]]; then
    echo "📦 Taking HOT backup (Jenkins running)..." | tee -a $LOG_FILE
    cp -r $JENKINS_HOME "$BACKUP_DIR/hot_backup"
    echo "✅ Hot backup done" | tee -a $LOG_FILE
  else
    echo "⏩ Hot backup already exists, skipping"
  fi
else
  echo "⏩ Jenkins not running → skipping hot backup"
fi
# -------------------------------
# STEP 3: STOP JENKINS
# -------------------------------
if systemctl is-active --quiet jenkins; then
  echo "🛑 Stopping Jenkins..." | tee -a $LOG_FILE
  systemctl stop jenkins
else
  echo "⏩ Jenkins already stopped"
fi

# -------------------------------
# STEP 4: COLD BACKUP (CONSISTENT)
# -------------------------------
if [[ ! -d "$BACKUP_DIR/cold_backup" ]]; then
  echo "📦 Taking COLD backup..." | tee -a $LOG_FILE
  cp -r $JENKINS_HOME "$BACKUP_DIR/cold_backup"
  cp -r /etc/sysconfig/jenkins "$BACKUP_DIR/" 2>/dev/null || true
  cp -r /usr/lib/systemd/system/jenkins.service "$BACKUP_DIR/" 2>/dev/null || true
  cp -r /usr/share/java/jenkins.war "$BACKUP_DIR/" 2>/dev/null || true
  echo "✅ Cold backup done" | tee -a $LOG_FILE
else
  echo "⏩ Cold backup already exists, skipping"
fi

# -------------------------------
# STEP 4.5: OPTIONAL PLUGIN UPGRADE
# -------------------------------

read -p "Do you want to upgrade plugins before Jenkins upgrade? (yes/no): " PLUGIN_UPGRADE

if [[ "$PLUGIN_UPGRADE" == "yes" ]]; then
  echo "🔌 Preparing plugin upgrade..." | tee -a $LOG_FILE

  CLI_JAR="/usr/local/bin/jenkins-plugin-cli"

  # Download plugin CLI if not present
  if [[ ! -f "$CLI_JAR" ]]; then
    echo "⬇️ Downloading jenkins-plugin-cli..." | tee -a $LOG_FILE
    curl -L -o "$CLI_JAR" https://github.com/jenkinsci/plugin-installation-manager-tool/releases/download/2.14.0/jenkins-plugin-manager-2.14.0.jar
    chmod +x "$CLI_JAR"
  else
    echo "⏩ jenkins-plugin-cli already exists"
  fi

  # Generate plugin list from existing plugins
  PLUGIN_LIST_FILE="$BACKUP_DIR/plugins.txt"

  echo "📦 Generating plugin list from existing installation..." | tee -a $LOG_FILE
  ls $JENKINS_HOME/plugins/*.jpi 2>/dev/null | \
    xargs -n1 basename | sed 's/.jpi//' > "$PLUGIN_LIST_FILE"

  # Backup plugins before upgrade
  if [[ ! -d "$BACKUP_DIR/plugins_backup" ]]; then
    echo "📦 Backing up plugins..." | tee -a $LOG_FILE
    cp -r $JENKINS_HOME/plugins "$BACKUP_DIR/plugins_backup"
  else
    echo "⏩ Plugin backup exists"
  fi

  echo "⬆️ Upgrading plugins..." | tee -a $LOG_FILE

  java -jar "$CLI_JAR" \
    --plugin-file "$PLUGIN_LIST_FILE" \
    --plugin-download-directory "$JENKINS_HOME/plugins" \
    --war /usr/share/java/jenkins.war \
    --latest true >> $LOG_FILE 2>&1

  echo "✅ Plugin upgrade completed" | tee -a $LOG_FILE

else
  echo "⏩ Skipping plugin upgrade"
fi

# -------------------------------
# STEP 5: CONFIGURE REPO (SAFE)
# -------------------------------

sudo wget -O /etc/yum.repos.d/jenkins.repo \
https://pkg.jenkins.io/rpm-stable/jenkins.repo


# -------------------------------
# STEP 6: CHECK JAVA VERSION
# -------------------------------
echo "🔍 Checking Java version..." | tee -a $LOG_FILE

JAVA_VER=$(java -version 2>&1 | head -n1 || true)

echo "Current Java: $JAVA_VER" | tee -a $LOG_FILE

if ! echo "$JAVA_VER" | grep -q "21"; then
  echo "⚠️ Java 17 not found. Installing Amazon Corretto 17..." | tee -a $LOG_FILE

  yum install -y java-17-amazon-corretto >> $LOG_FILE 2>&1

  # Set as default
  alternatives --set java /usr/lib/jvm/java-17-amazon-corretto*/bin/java

  JAVA_PATH=$(readlink -f /usr/bin/java | sed "s:/bin/java::")

  echo "🔧 Setting Jenkins Java path..." | tee -a $LOG_FILE

  sed -i "s|^JENKINS_JAVA_CMD=.*|JENKINS_JAVA_CMD=$JAVA_PATH/bin/java|" \
    /etc/sysconfig/jenkins 2>/dev/null || true

  echo "✅ Java 17 configured" | tee -a $LOG_FILE
else
  echo "⏩ Java 17 already present"
fi

# -------------------------------
# STEP 7: UPGRADE JENKINS
# -------------------------------
CURRENT_VER=$(rpm -qa | grep jenkins || true)

echo "🔍 Current Jenkins: $CURRENT_VER" | tee -a $LOG_FILE

if echo "$CURRENT_VER" | grep -q "2.541"; then
  echo "⏩ Jenkins already upgraded, skipping"
else
  echo "⬆️ Upgrading Jenkins..." | tee -a $LOG_FILE
  yum clean all
  yum makecache
  yum update -y jenkins >> $LOG_FILE 2>&1
  echo "✅ Jenkins upgraded" | tee -a $LOG_FILE
fi

# ------------------------------
# STEP 7.5: UPDATE Jenkins SERVICE FILE
# ------------------------------

# Detect Java path
JAVA_BIN=$(readlink -f /usr/bin/java)

echo "Detected Java: $JAVA_BIN" | tee -a $LOG_FILE

# Update Jenkins config safely
if grep -q "^JENKINS_JAVA_CMD=" /etc/sysconfig/jenkins; then
  sed -i "s|^JENKINS_JAVA_CMD=.*|JENKINS_JAVA_CMD=$JAVA_BIN|" /etc/sysconfig/jenkins
else
  echo "JENKINS_JAVA_CMD=$JAVA_BIN" >> /etc/sysconfig/jenkins
fi

echo "✅ Jenkins will use: $JAVA_BIN" | tee -a $LOG_FILE

# -------------------------------
# STEP 8: START JENKINS
# -------------------------------
echo "🚀 Starting Jenkins..." | tee -a $LOG_FILE

systemctl daemon-reexec
systemctl daemon-reload
systemctl start jenkins

sleep 15

# -------------------------------
# STEP 9: VERIFY
# -------------------------------
if systemctl is-active --quiet jenkins; then
  echo "✅ Jenkins is running" | tee -a $LOG_FILE
else
  echo "❌ Jenkins failed to start" | tee -a $LOG_FILE
  journalctl -u jenkins -n 50 --no-pager
  exit 1
fi

# -------------------------------
# FINAL
# -------------------------------
echo ""
echo "🎉 Jenkins upgrade SUCCESS"
echo "🌐 Verify UI + plugins"

# -------------------------------
# ROLLBACK
# -------------------------------
echo ""
echo "🛑 ROLLBACK:"
echo "systemctl stop jenkins"
echo "rm -rf $JENKINS_HOME"
echo "cp -r $BACKUP_DIR/cold_backup $JENKINS_HOME"
echo "chown -R jenkins:jenkins $JENKINS_HOME"
echo "systemctl start jenkins"