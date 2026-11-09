# CSCE 412 — Project 5: NexaCloud Multi-Agent Cloud Deployment

A secure static website deployed on AWS using a custom multi-agent AI system.
Live site: [https://csce412advaithr07.me](https://csce412advaithr07.me)

---

## Project Overview

This project implements a four-agent AI system that plans, deploys, validates,
and diagnoses a secure static website hosted on AWS. The website (NexaCloud) is
delivered through CloudFront, stored in a private S3 bucket, secured with an
ACM TLS certificate, and routed via Route 53.

---

## Architecture

Analyst → outputs/plan.json → Deployer → outputs/deploy_result.json
↓
Validator → outputs/validation.json
↓
Diagnostician → outputs/diagnosis.json