use std::iter::once;

use chroma_test::benchmark::{bench, tokio_multi_thread};
use chroma_test::log::{offset_as_id, random_document, random_embedding, LogGenerator};
use chroma_test::segment::CompactSegment;
use chroma_types::{Operation, OperationRecord, UpdateMetadataValue};
use criterion::Criterion;
use criterion::{criterion_group, criterion_main};

const DOCUMENT_LENGTH: usize = 64;
const EMBEDDING_DIMENSION: usize = 6;
const LIMIT: usize = 100;

fn log_generator(id: usize) -> OperationRecord {
    OperationRecord {
        id: offset_as_id(id),
        embedding: Some(random_embedding(EMBEDDING_DIMENSION)),
        encoding: None,
        metadata: Some(once(("val".to_string(), UpdateMetadataValue::Int(id as i64))).collect()),
        document: Some(random_document(DOCUMENT_LENGTH)),
        operation: Operation::Add,
    }
}

fn bench_limit(criterion: &mut Criterion) {
    let runtime = tokio_multi_thread();
    let logen = LogGenerator {
        generator: log_generator,
    };

    let routine = |_limit_input| async move {
        // TODO: Run limit operator
    };

    for record_count in [1000, 10000, 100000] {
        let mut compact = CompactSegment::default();
        runtime.block_on(async { compact.populate_with_generator(record_count, &logen).await });

        for offset in [0, record_count / 2, record_count - LIMIT] {
            let setup = || {
                // TODO: Construct limit input
            };
            bench(
                format!("limit-{}-{}", record_count, offset).as_str(),
                criterion,
                &runtime,
                setup,
                routine,
            );
        }
    }
}

criterion_group!(benches, bench_limit);
criterion_main!(benches);