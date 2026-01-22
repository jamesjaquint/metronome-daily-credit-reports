"""
AWS Lambda handler for Metronome Daily Credit Report.
Wraps credit_report.py for Lambda execution.
"""
import os
import json

# Set up environment before importing credit_report
# Lambda passes secrets via environment variables

def handler(event, context):
    """Lambda entry point."""
    # Import here to ensure env vars are set first
    from credit_report import main, load_customers

    try:
        # Run the report
        main()

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Credit report completed successfully',
                'customers': len(load_customers())
            })
        }
    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e)
            })
        }
