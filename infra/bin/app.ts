#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { EvacAgentStack } from '../lib/evac-agent-stack';

const app = new cdk.App();
new EvacAgentStack(app, 'EvacAgentDemo', {
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'ap-northeast-1' },
});
