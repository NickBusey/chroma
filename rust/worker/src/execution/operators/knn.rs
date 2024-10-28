use std::cmp::Ordering;

#[derive(Clone, Debug)]
pub struct Distance {
    pub oid: u32,
    pub measure: f32,
}

impl PartialEq for Distance {
    fn eq(&self, other: &Self) -> bool {
        self.measure.eq(&other.measure)
    }
}

impl Eq for Distance {}

impl Ord for Distance {
    fn cmp(&self, other: &Self) -> Ordering {
        self.measure.total_cmp(&other.measure)
    }
}

impl PartialOrd for Distance {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

pub fn normalize(vector: &[f32]) -> Vec<f32> {
    let norm = vector.iter().map(|x| x * x).sum::<f32>().sqrt();
    vector.iter().map(|x| x / (norm + 1e-32)).collect()
}

#[derive(Clone, Debug)]
pub struct KnnOperator {
    pub embedding: Vec<f32>,
    pub fetch: u32,
}