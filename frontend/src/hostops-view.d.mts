export function hostState(value?: string): {label:string;color:string}
export function serviceState(value?: string): {label:string;color:string}
export function diskView(disk?: {total_bytes:number;used_bytes:number;free_bytes:number}|null): {percent:number;free:string;total:string}|null
