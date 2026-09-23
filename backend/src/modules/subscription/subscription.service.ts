import { Request, Response } from 'express';

import { Injectable } from '@nestjs/common';
import { Logger } from '@nestjs/common';

import { TRequestTemplateTypeKeys } from '@remnawave/backend-contract';

import { AxiosService } from '@common/axios/axios.service';

import { applyXOut } from './x-out';

@Injectable()
export class SubscriptionService {
    private readonly logger = new Logger(SubscriptionService.name);

    constructor(private readonly axiosService: AxiosService) {}

    public async serveSubscriptionPage(
        clientIp: string,
        req: Request,
        res: Response,
        shortUuid: string,
        clientType?: TRequestTemplateTypeKeys,
    ): Promise<void> {
        try {
            const subscriptionDataResponse = await this.axiosService.getSubscription(
                clientIp,
                shortUuid,
                req.headers,
                !!clientType,
                clientType,
            );

            if (!subscriptionDataResponse) {
                res.socket?.destroy();
                return;
            }

            if (subscriptionDataResponse.headers) {
                res.set(subscriptionDataResponse.headers);
            }

            const transformed = applyXOut(subscriptionDataResponse.subscription);
            if (transformed.warning) this.logger.warn(transformed.warning);
            if (transformed.modified) {
                for (const header of [
                    'content-length',
                    'content-encoding',
                    'etag',
                    'last-modified',
                    'content-md5',
                    'digest',
                ]) {
                    res.removeHeader(header);
                }
                res.set('Cache-Control', 'private, no-store');
            }
            res.status(200).send(transformed.body);
            return;
        } catch (error) {
            this.logger.error('Error in serveSubscriptionPage', error);

            res.socket?.destroy();
            return;
        }
    }
}
