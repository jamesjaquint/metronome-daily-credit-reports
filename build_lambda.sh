#!/bin/bash
# Build Lambda deployment package

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/lambda_build"
PACKAGE_FILE="$SCRIPT_DIR/lambda_package.zip"

echo "Building Lambda package..."

# Clean up
rm -rf "$BUILD_DIR"
rm -f "$PACKAGE_FILE"

# Create build directory
mkdir -p "$BUILD_DIR"

# Install dependencies
echo "Installing dependencies..."
pip install -r "$SCRIPT_DIR/requirements.txt" -t "$BUILD_DIR" --quiet

# Copy source files
echo "Copying source files..."
cp "$SCRIPT_DIR/lambda_handler.py" "$BUILD_DIR/"
cp "$SCRIPT_DIR/credit_report.py" "$BUILD_DIR/"
cp "$SCRIPT_DIR/customers.json" "$BUILD_DIR/"

# Create zip
echo "Creating zip package..."
cd "$BUILD_DIR"
zip -r "$PACKAGE_FILE" . -x "*.pyc" -x "__pycache__/*" -x "*.dist-info/*" > /dev/null

# Cleanup
rm -rf "$BUILD_DIR"

echo "Package created: $PACKAGE_FILE"
ls -lh "$PACKAGE_FILE"
