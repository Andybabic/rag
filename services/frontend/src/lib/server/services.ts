import { env } from '$env/dynamic/private';

export const SERVICES = {
	evaluation: env.EVALUATION_SERVICE_URL ?? 'http://evaluation:8005',
	cleaning: env.CLEANING_SERVICE_URL ?? 'http://cleaning:8001',
	dataStructure: env.DATA_STRUCTURE_SERVICE_URL ?? 'http://data_structure:8002',
	embedding: env.EMBEDDING_SERVICE_URL ?? 'http://embedding:8003',
	vectordb: env.VECTORDB_SERVICE_URL ?? 'http://vectordb:8004'
};
