import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as nodejs from 'aws-cdk-lib/aws-lambda-nodejs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';

const ROOT = path.join(__dirname, '..', '..');
// エージェントを zip（コード＋依存ライブラリ＋避難所データ）にまとめる。Docker は使わず、
// uv で AgentCore Runtime の実行環境（Linux / ARM64・Python 3.12）向けのライブラリを取得する。
function agentCodeAsset(): agentcore.AgentRuntimeArtifact {
  const agentDir = path.join(ROOT, 'agent');
  return agentcore.AgentRuntimeArtifact.fromCodeAsset({
    path: agentDir,
    runtime: agentcore.AgentCoreRuntime.PYTHON_3_12,
    entrypoint: ['main.py'],
    exclude: ['.venv', 'tests', '__pycache__', 'Dockerfile'],
    assetHashType: cdk.AssetHashType.OUTPUT,
    bundling: {
      image: cdk.DockerImage.fromRegistry('unused'), // local で必ずバンドルするため使われない
      local: {
        tryBundle(outputDir: string) {
          execFileSync('uv', [
            'pip', 'install', '--quiet',
            '--target', outputDir,
            '--python-platform', 'aarch64-manylinux_2_28',
            '--python-version', '3.12',
            '--only-binary=:all:',
            '-r', path.join(agentDir, 'requirements.txt'),
          ], { stdio: 'inherit' });
          for (const f of fs.readdirSync(agentDir).filter((n) => n.endsWith('.py'))) {
            fs.copyFileSync(path.join(agentDir, f), path.join(outputDir, f));
          }
          fs.mkdirSync(path.join(outputDir, 'data'), { recursive: true });
          for (const f of ['shelters.json', 'rivers.json']) {
            fs.copyFileSync(path.join(ROOT, 'data', f), path.join(outputDir, 'data', f));
          }
          return true;
        },
      },
    },
  });
}

// サーバ側には状態を持たない構成。世帯情報と避難計画は PWA の端末内（localStorage）に保存する。
// 警報はデモ用の疑似警報（data/alerts.json）を画面が読み込むだけなので、AWS 側には警報用のリソースがない。
export class EvacAgentStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const modelId: string = this.node.tryGetContext('modelId');
    const thinkingDisplay: string = this.node.tryGetContext('thinkingDisplay') ?? '';

    // ---- AI エージェント：Strands Agents を AgentCore Runtime で実行 ----
    const runtime = new agentcore.Runtime(this, 'EvacAgentRuntime', {
      runtimeName: 'evac_plan_agent',
      description: '警報を受けて、利用者の状況に合わせた避難計画を作るエージェント',
      agentRuntimeArtifact: agentCodeAsset(),
      environmentVariables: {
        MODEL_ID: modelId,
        THINKING_DISPLAY: thinkingDisplay,
      },
    });
    // 使うモデルだけを呼べるようにする（最小権限）。
    // 推論プロファイル（jp. / global. など）経由では、宛先リージョンの基盤モデルにも許可が必要
    const foundationModelId = modelId.replace(/^(global|jp|apac|us|eu|au)\./, '');
    runtime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:aws:bedrock:*::foundation-model/${foundationModelId}`,
          `arn:aws:bedrock:*:${this.account}:inference-profile/${modelId}`,
        ],
      }),
    );

    // ---- エージェント呼び出し：Lambda Function URL（レスポンスストリーミング）----
    const agentProxy = new nodejs.NodejsFunction(this, 'AgentProxyFn', {
      entry: path.join(__dirname, '..', 'lambda', 'agent-proxy.ts'),
      runtime: lambda.Runtime.NODEJS_22_X,
      architecture: lambda.Architecture.ARM_64,
      timeout: cdk.Duration.minutes(15),
      memorySize: 256,
      // 同時実行数を絞って、Bedrock の利用額が膨らまないようにする
      reservedConcurrentExecutions: 10,
      environment: { AGENT_RUNTIME_ARN: runtime.agentRuntimeArn },
      // AgentCore の SDK はランタイム同梱版に含まれない場合があるので、バンドルに含める
      bundling: { format: nodejs.OutputFormat.CJS, minify: true, externalModules: [] },
    });
    runtime.grantInvoke(agentProxy);
    // IAM 認証にして、CloudFront（OAC）経由でしか呼べないようにする。画面と同じドメインの /api/agent になるので CORS も不要
    const agentUrl = agentProxy.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.AWS_IAM,
      invokeMode: lambda.InvokeMode.RESPONSE_STREAM,
    });

    // ---- 画面（PWA）：S3 + CloudFront ----
    const siteBucket = new s3.Bucket(this, 'SiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });
    // ブラウザ向けのセキュリティヘッダー。CSP は、画面が読み込む外部の配信元（地図・住所検索・CDN・フォント）だけを許可する
    const gsi = 'https://cyberjapandata.gsi.go.jp https://disaportaldata.gsi.go.jp';
    const csp = [
      "default-src 'self'",
      "script-src 'self' https://cdnjs.cloudflare.com",
      // Leaflet が地図の要素に style 属性を使うため、スタイルだけ 'unsafe-inline' を許可する
      "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com",
      'font-src https://fonts.gstatic.com',
      `img-src 'self' data: ${gsi} https://cdnjs.cloudflare.com`,
      `connect-src 'self' ${gsi} https://msearch.gsi.go.jp https://mreversegeocoder.gsi.go.jp https://cdnjs.cloudflare.com https://fonts.googleapis.com https://fonts.gstatic.com`,
      "worker-src 'self'",
      "manifest-src 'self'",
      "object-src 'none'",
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'none'",
    ].join('; ');
    const securityHeaders = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      securityHeadersBehavior: {
        contentSecurityPolicy: { contentSecurityPolicy: csp, override: true },
        strictTransportSecurity: { accessControlMaxAge: cdk.Duration.days(730), includeSubdomains: true, override: true },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: { referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override: true },
      },
      customHeadersBehavior: {
        customHeaders: [
          // 位置情報はこの画面だけで使う。カメラやマイクは使わない
          { header: 'Permissions-Policy', value: 'geolocation=(self), camera=(), microphone=(), payment=()', override: true },
        ],
      },
    });

    const distribution = new cloudfront.Distribution(this, 'SiteDistribution', {
      defaultRootObject: 'index.html',
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        responseHeadersPolicy: securityHeaders,
      },
      additionalBehaviors: {
        // エージェントの呼び出し（POST のみ）。キャッシュせず、そのままストリーミングで返す
        '/api/agent': {
          origin: origins.FunctionUrlOrigin.withOriginAccessControl(agentUrl, { readTimeout: cdk.Duration.seconds(60) }),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
          responseHeadersPolicy: securityHeaders,
        },
      },
    });
    new s3deploy.BucketDeployment(this, 'DeploySite', {
      destinationBucket: siteBucket,
      sources: [
        s3deploy.Source.asset(path.join(ROOT, 'frontend')),
        s3deploy.Source.jsonData('config.json', { agentUrl: '/api/agent' }),
      ],
      prune: false,
      distribution,
    });
    new s3deploy.BucketDeployment(this, 'DeployData', {
      destinationBucket: siteBucket,
      destinationKeyPrefix: 'data/',
      sources: [s3deploy.Source.asset(path.join(ROOT, 'data'), { exclude: ['ibaraki_shelter.csv'] })],
      prune: false,
    });

    new cdk.CfnOutput(this, 'SiteUrl', { value: `https://${distribution.distributionDomainName}` });
    new cdk.CfnOutput(this, 'AgentRuntimeArn', { value: runtime.agentRuntimeArn });
  }
}
